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
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / "bin"
START = BIN / "qb-start"
HM_MODULE = Path(__file__).resolve().parents[1] / "hm-module.nix"

STARTED, MISUSE = 0, 2
NOT_ENABLED, NOT_ALLOWED, AT_CAP = 3, 4, 5
PACED, FULL, HELD = 6, 7, 8
COULD_NOT_START = 9
AT_FLEET_CAP = 10


def dial(name: str, value: object, repo: str | None = None,
         set_by: str = "human/rich", set_via: str | None = None) -> dict:
    """One row as `GET /dials` returns it. Fleet scope by default, which is the only
    scope either spawn dial can mean — `repo` is for the row that should not exist.

    `set_via` defaults to ABSENT rather than to `"edge"`, and that is the honest
    default for this helper: it is what a row written before the column existed
    looks like, and every ceiling test that predates #591 is asserting about
    exactly such a row. Pass `set_via="agent"` for the case that dial cannot take.
    """
    row = {"dial": name, "value": value,
           "scope": "repo" if repo else "fleet", "repo": repo,
           "reason": "a test", "set_by": set_by, "expires_at": None}
    if set_via is not None:
        row["set_via"] = set_via
    return row


def agents(n: int, subagents: int = 0) -> dict:
    """`GET /active`'s shape with `n` live top-level agents beside `subagents` of
    their fan-out, which is deliberately not counted."""
    return {"agents": [{"session": f"s{i}"} for i in range(n)],
            "subagents": [{"agent_id": f"a{i}"} for i in range(subagents)]}

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
            plan_next: object = "unset", dials: object = None, active: object = None,
            pace: int = 0, admit: int = 0, claim: int = 0, release: int = 0,
            tmux_exit: int | None = 0, new_window_exit: int | None = None,
            set_option_exit: int | None = None,
            kill_pane_exit: int | None = None) -> dict:
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
    reads = tmp_path / "reads.jsonl"
    (stub / "qbdata.py").write_text(f"""
import json

if {explode!r}:
    raise ImportError("a machine that has not opted in must not need a board client")


class _Client:
    def post(self, path, body):
        with open({str(posts)!r}, "a") as fh:
            fh.write(json.dumps({{"path": path, "body": body}}) + "\\n")
        return {{}}

    def get(self, path, params=None, timeout=None):
        # Recorded, so a test can assert what was asked and with what bound — the
        # dial read is on the hot path and the point of its short timeout is that
        # it is short.
        with open({str(reads)!r}, "a") as fh:
            fh.write(json.dumps({{"path": path, "params": params,
                                  "timeout": timeout}}) + "\\n")
        if path == "/dials":
            # `None` is a board with no dial layer at all — the pre-#563 board, and
            # the ordinary state of a fleet that has set none. qb-start's ceiling
            # then comes off the policy file, which is the fail-open property.
            if {dials!r} is None:
                raise AttributeError("this stub board holds no dials")
            # A STRING is how a test asks for a body that is not the shape the
            # caller expects: `"list"` answers a JSON array, which is truthy and has
            # no `.get`, and `"dials-not-a-list"` answers an object whose one key is
            # the wrong type. Both are readable answers that mean nothing.
            if {dials!r} == "list":
                return [1, 2]
            if {dials!r} == "dials-not-a-list":
                return {{"dials": "several"}}
            return {{"dials": {dials!r}}}
        if path == "/active":
            if {active!r} is None:
                raise AttributeError("this stub board answers no /active")
            if {active!r} == "list":
                return [1, 2]
            return {active!r}
        # `unset` means this stub has no `get` worth speaking of, which is the
        # pre-#541 board: qb-start's plan question then fails OPEN and says so.
        if {plan_next!r} == "unset":
            raise AttributeError("this stub board answers no reads")
        return {{"next": {plan_next!r} or None}}


def repo_slug(path="."):
    # None for a path that is not a checkout, which is what the real one answers and
    # what `read_ceilings` degrades on: the repo buys the diagnostic, not the number.
    import os.path
    return "acme/widget" if os.path.isdir(os.path.join(path, ".git")) else None


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
            'if [ "$1" = "kill-pane" ]; then\n'
            f'  exit {tmux_exit if kill_pane_exit is None else kill_pane_exit}\n'
            "fi\n"
            f"exit {tmux_exit}\n")
        (tools / "tmux").chmod(0o755)

    # $XDG_CONFIG_HOME, because there is no override to point at a file: the
    # resolution under test is the real one, and it is the same one `qb-env` and
    # `qbdata` use. A `$QUARTERBACK_SPAWN` existed here until the
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
            "tools": tools, "log": log, "posts": posts, "reads": reads,
            "root": tmp_path}


def run(box: dict, *args: str, repo_path: str | None = None, tmux: str = "",
        env: dict | None = None, cwd: Path | None = None):
    """`qb-start` inside the sandbox. `tmux` is what $TMUX is set to — empty means
    there is no multiplexer, which is a different answer from a broken one."""
    over = {"XDG_CONFIG_HOME": str(box["config"]), **(env or {})}
    if tmux:
        over["TMUX"] = tmux
    # `box["tools"]` and a toolbox of named binaries, not the developer's PATH
    # (#528). `qb-start` resolves its neighbours with `shutil.which`, so the
    # inherited PATH found the INSTALLED `qb-claim` the moment a test deleted the
    # stub — `test_a_qb_claim_that_is_not_installed_refuses_the_spawn` ran a real
    # `qb-claim issue 277` against a throwaway repo, measured once per run, and
    # got its verdict from that tool failing for an unrelated reason rather than
    # from the tool being missing. `sibling()`'s `${script dir}/qb-claim` fallback
    # cannot restore it either: the copy under test sits in `stub/`, which holds
    # only what this fixture wrote.
    #
    # `sleep` is on the list because tmux hands the pane it opens THIS PATH, and
    # the real-tmux tests below use `sleep 60` as their still-running agent —
    # measured: without it the pane's command dies at once and `@qb_spawn_ended`
    # is set before the test can read it.
    where = _path_sandbox.sandbox_env(
        box["root"], box["tools"], tools=("git", "sh", "bash", "sleep"), **over)
    if not tmux:
        where.pop("TMUX", None)
    where.pop("TMUX_PANE", None)
    got = subprocess.run(
        [sys.executable, str(box["script"]),
         "--repo-path", repo_path or str(box["repo"]), *args],
        capture_output=True, text=True, env=where, cwd=str(cwd) if cwd else None)
    got.ran = (box["log"].read_text().splitlines() if box["log"].exists() else [])
    got.posts = [json.loads(ln) for ln in
                 (box["posts"].read_text().splitlines() if box["posts"].exists() else [])]
    got.reads = [json.loads(ln) for ln in
                 (box["reads"].read_text().splitlines() if box["reads"].exists() else [])]
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


@pytest.mark.parametrize("home", ["", "relative/path"])
def test_with_no_config_home_the_gate_is_not_resolved_against_the_checkout(home, tmp_path):
    """`os.path.join("", ".config")` is `.config`, so the obvious spelling resolves
    the one file that can say yes against the CURRENT DIRECTORY — and a repository
    shipping `.config/quarterback/spawn.json` would then be granting itself the
    permission. The exact party this gate excludes, reached by a variable being
    absent rather than by one being set."""
    box = sandbox(tmp_path, policy=ENABLED)
    planted = box["repo"] / ".config" / "quarterback"
    planted.mkdir(parents=True)
    (planted / "spawn.json").write_text(json.dumps(ENABLED))
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0",
              env={"XDG_CONFIG_HOME": home, "HOME": home}, cwd=box["repo"])
    assert got.returncode == NOT_ENABLED, got.stderr
    assert got.ran == []


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


# ------------------------------- what pulled it, and asking before you pull it

def test_a_caller_can_ask_what_this_machine_will_start_without_asking_for_one(tmp_path):
    """`--policy` is the question a trigger with a button on it has to ask BEFORE
    the click. It starts nothing, claims nothing, posts nothing — and `explode` is
    left ON here, so the board client could not have been imported at all, which
    makes this the FAIL-OPEN case as well: the ceiling is the policy file's, named
    as the policy file's, and nothing about the answer degrades (#563)."""
    box = sandbox(tmp_path, policy=ENABLED)
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    answer = json.loads(got.stdout)
    assert answer["enabled"] is True
    assert answer["commands"] == ["/fix-issue", "/panel-review-pr"]
    assert answer["max_sessions"] == 2
    assert answer["max_sessions_source"] == "policy"
    assert answer["max_sessions_fleet"] is None
    assert got.ran == [], f"asking the question consulted {got.ran}"
    assert got.posts == []


def test_asking_a_machine_that_never_opted_in_gets_the_refusal_and_the_remedy(tmp_path):
    """The answer a button needs in order to refuse usefully rather than to look
    live, take the click and only then explain itself."""
    got = run(sandbox(tmp_path), "--policy", "--json")
    assert got.returncode == NOT_ENABLED
    answer = json.loads(got.stdout)
    assert answer["enabled"] is False
    assert answer["commands"] == []
    assert "programs.quarterback-harness.spawn.enable" in answer["reason"]
    assert got.ran == []


def test_the_answer_cannot_widen_the_compiled_in_set_either(tmp_path):
    """A caller reading `commands` off this and drawing a button for each would
    otherwise be handed the policy's wishes rather than what would actually run —
    and then the button it drew would be refused at exit 4."""
    box = sandbox(tmp_path, policy={"enabled": True,
                                    "commands": ["/anything-i-like", "/fix-issue"]})
    got = run(box, "--policy", "--json")
    assert got.returncode == STARTED
    assert json.loads(got.stdout)["commands"] == ["/fix-issue"]


def test_what_pulled_the_spawn_is_on_the_claim_the_post_and_the_pane(tmp_path):
    """The question a session nobody started raises, and the only one. Recorded in
    three places on purpose: the claim is what `qb-claimed` shows, the post
    outlives the pane, and the pane is what somebody who found a running window
    can interrogate without leaving tmux."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", "--via", "dash", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    claim = next(ln for ln in got.ran if ln.startswith("qb-claim"))
    assert "via dash" in claim
    body = got.posts[0]["body"]
    assert "via dash" in body["summary"]
    assert "asked for by: dash" in body["detail"]
    assert any("@qb_spawn_via dash" in ln for ln in got.ran), \
        f"the pane should wear its provenance: {got.ran}"


def test_a_provenance_stamp_that_fails_does_not_throw_the_session_away(tmp_path):
    """It is outside the checked three deliberately. `@qb_spawn`, `@qb_session` and
    `@qb_label` are what make a spawn countable and endable, so a window that lost
    one is closed rather than run; provenance is a label on a session that is
    already counted, claimed and endable, and the board post says who asked for it
    whatever the pane wears."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    # A tmux whose `set-option` refuses ONLY the provenance stamp.
    (box["tools"] / "tmux").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "tmux $*" >> {box["log"]}\n'
        'if [ "$1" = "new-window" ]; then printf "%s\\n" "%9"; exit 0; fi\n'
        'if [ "$1" = "set-option" ]; then\n'
        '  case "$*" in *@qb_spawn_via*) exit 1;; esac\n'
        '  exit 0\n'
        "fi\n"
        "exit 0\n")
    (box["tools"] / "tmux").chmod(0o755)
    got = run(box, "/fix-issue", "277", "--via", "dash", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    assert not any(ln.startswith("tmux kill-pane") for ln in got.ran)


def test_a_trigger_the_script_does_not_name_is_refused(tmp_path):
    """Closed for `SPAWNABLE`'s reason: a provenance field its caller fills in
    freely is one that can be made to say a human did it."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", "--via", "a-human-obviously", tmux="/tmp/fake,1,0")
    assert got.returncode == MISUSE, got.stderr
    assert got.ran == []


def test_a_refusal_records_what_asked_for_it_too(tmp_path):
    """"The dash was refused forty times today" is a sentence somebody should be
    able to read off the board, and a refusal that did not say who was refused
    cannot say it."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, admit=1)
    got = run(box, "/fix-issue", "277", "--via", "dash", tmux="/tmp/fake,1,0")
    assert got.returncode == FULL
    assert "asked for by: dash" in got.posts[0]["body"]["detail"]


def test_with_no_via_a_spawn_is_recorded_as_a_person_at_a_prompt(tmp_path):
    """The default, and it is the caller that needs no explaining."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert "via cli" in next(ln for ln in got.ran if ln.startswith("qb-claim"))
    assert "asked for by: cli" in got.posts[0]["body"]["detail"]


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


def test_a_release_that_failed_is_not_reported_as_a_claim_handed_back(tmp_path):
    """The worse half of two failures: the slot is held AND the only record says it
    is free, so nobody goes looking. The TTL is still underneath it, and eight hours
    is exactly the wait this line exists to save somebody."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, new_window_exit=1,
                  release=2)
    got = run(box, "/fix-issue", "277", "--json", tmux="/tmp/fake,1,0")
    assert got.returncode == COULD_NOT_START
    assert "could NOT be handed back" in got.stderr
    assert json.loads(got.stdout)["claim_released"] is False
    assert "could NOT be handed back" in got.posts[-1]["body"]["detail"]


def test_a_window_that_could_not_be_closed_says_so_and_names_the_pane(tmp_path):
    """An unstamped agent running against a claim about to be handed back is the one
    state this whole file is arranged to prevent. Saying "closed again" over it
    would hide precisely the thing somebody has to go and do by hand."""
    box = sandbox(tmp_path, policy=ENABLED, explode=False, set_option_exit=1,
                  kill_pane_exit=1)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == COULD_NOT_START
    assert "COULD NOT BE CLOSED" in got.stderr
    assert "tmux kill-pane -t %9" in got.stderr


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


def test_every_spawnable_command_says_what_it_claims_even_when_that_is_nothing():
    """A command whose unit of work nobody has worked out is a command that cannot
    be started — not one that is started uncounted.

    Restated for #541 rather than relaxed. The old form read the ref kind out of
    the table and required one for every entry, which WAS the arity check as long
    as every command took a number. `/get-involved` takes none, so the invariant
    is now: every entry names a kind, or names `None` deliberately — and `None`
    means the session claims its own item once it has read the plan, never that
    nobody thought about it. An entry the pattern below cannot match at all still
    fails, which is the half that matters."""
    body = START.read_text()
    block = body.split("SPAWNABLE = {", 1)[1].split("\n}", 1)[0]
    kinds = dict(re.findall(r'"(/[a-z-]+)":\s*Spawnable\(\s*(None|"[a-z]+")', block))
    assert set(kinds) == read_spawnable(), "an entry nobody declared an arity for"
    assert set(kinds.values()) <= {'"issue"', '"pr"', "None"}
    claimless = {c for c, k in kinds.items() if k == "None"}
    assert claimless == {"/get-involved"}, (
        "a new claimless command needs the claim-skip path and its own test, not "
        "just a table entry")


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


# ------------------------------------------------- #541: a command with no number

#: A policy that admits `/get-involved` honestly — it and everything it dispatches
#: into. Written out rather than computed, so a test cannot pass because the
#: production table and the fixture drifted the same way.
INVOLVED_OK = {"enabled": True, "max_sessions": 2, "commands": [
    "/get-involved", "/fix-issue", "/fix-and-land", "/review-pr", "/panel-review-pr"]}


def test_a_numberless_command_is_accepted_and_briefs_without_one(tmp_path):
    """`/get-involved` reads the plan and self-selects, so there is no number to
    pass. The brief must be the command ALONE — not `"/get-involved "` and not
    `"/get-involved None"`, both of which a slash-command parser matching on
    equality would miss."""
    box = sandbox(tmp_path, policy=INVOLVED_OK, explode=False, plan_next={"item_id": "x"})
    got = run(box, "--dry-run", "/get-involved")
    assert got.returncode == STARTED, got.stderr
    assert "-- /get-involved" in got.stderr
    assert "/get-involved None" not in got.stderr


def test_a_numbered_command_still_refuses_a_missing_number(tmp_path):
    """The arity moved into the table; it did not go away."""
    box = sandbox(tmp_path, policy=INVOLVED_OK, explode=False)
    got = run(box, "--dry-run", "/fix-issue")
    assert got.returncode == MISUSE, got.stderr
    assert "takes an issue or PR number" in got.stderr


def test_a_number_handed_to_a_numberless_command_is_a_misuse(tmp_path):
    """The other direction, and it is not pedantry: a caller passing a number
    believes it has aimed this at something specific. Silently dropping it would
    start a session that picks its own work while the caller's records say
    otherwise."""
    box = sandbox(tmp_path, policy=INVOLVED_OK, explode=False)
    got = run(box, "--dry-run", "/get-involved", "7")
    assert got.returncode == MISUSE, got.stderr
    assert "takes no number" in got.stderr


def test_the_claimless_path_takes_no_claim_and_releases_none(tmp_path):
    """The work interlock moves inside the session (`plan_claim`), because which
    item it takes is not known until it has read the plan. What must NOT happen is
    a claim on the wrong thing — or a release of one that was never taken, on a
    spawn that fails."""
    box = sandbox(tmp_path, policy=INVOLVED_OK, explode=False,
                  plan_next={"item_id": "x"}, new_window_exit=1)
    got = run(box, "/get-involved", tmux="/tmp/fake,1,0")
    assert got.returncode == COULD_NOT_START, got.stderr
    assert not [c for c in got.ran if c[0] in ("qb-claim", "qb-release")], got.ran
    assert "no claim was taken" in got.stderr


def test_a_policy_allowing_get_involved_but_not_what_it_dispatches_is_refused(tmp_path):
    """The second lock, kept true in fact rather than in form. `/get-involved`
    runs `/fix-issue` one hop along, so a machine that allows the first and
    withholds the second gets the second anyway — and its operator has no way to
    see it, because `--policy` reports the allowlist."""
    policy = {"enabled": True, "commands": ["/get-involved", "/review-pr",
                                            "/panel-review-pr", "/fix-and-land"]}
    box = sandbox(tmp_path, policy=policy, explode=False)
    got = run(box, "--dry-run", "/get-involved")
    assert got.returncode == NOT_ALLOWED, got.stderr
    assert "/fix-issue" in got.stderr and "one hop along" in got.stderr


def test_policy_reports_a_command_it_lists_but_refuses(tmp_path):
    """A file that disagrees with itself is the one thing an operator cannot read
    off the file."""
    policy = {"enabled": True, "commands": ["/get-involved", "/review-pr",
                                            "/panel-review-pr", "/fix-and-land"]}
    got = run(sandbox(tmp_path, policy=policy, explode=False), "--policy")
    assert got.returncode == STARTED, got.stderr
    assert "listed but refused" in got.stderr
    assert "/get-involved" in got.stderr


def test_dry_run_prints_a_claim_line_for_a_numbered_command_and_not_otherwise(tmp_path):
    box = sandbox(tmp_path, policy=INVOLVED_OK, explode=False, plan_next={"item_id": "x"})
    numbered = run(box, "--dry-run", "/fix-issue", "277")
    assert "claim:    issue 277" in numbered.stderr, numbered.stderr
    involved = run(box, "--dry-run", "/get-involved")
    assert "claim:    none" in involved.stderr, involved.stderr


def test_a_plan_with_nothing_free_refuses_before_a_session_exists(tmp_path):
    """The refusal that costs nothing. Without it the session starts, reads the
    plan, finds the same nothing and stops — correct, and a whole session spent to
    reach it. At the tail of a drain that is the common case."""
    box = sandbox(tmp_path, policy=INVOLVED_OK, explode=False, plan_next=None)
    got = run(box, "/get-involved", tmux="/tmp/fake,1,0")
    assert got.returncode == HELD, got.stderr
    assert "nothing on the plan is free" in got.stderr
    assert not [c for c in got.ran if c[0] == "qb-claim"], got.ran


def test_a_board_that_cannot_answer_the_plan_question_fails_OPEN(tmp_path):
    """The one gate here that does not fail closed, and the asymmetry is the point:
    the gates above it are counting a resource, and this one is only avoiding a
    wasted session. A board that did not answer has said nothing about the plan,
    and refusing on silence would make an unreachable board look like a finished
    one."""
    box = sandbox(tmp_path, policy=INVOLVED_OK, explode=False,  # `unset`: no reads
                  new_window_exit=1)
    got = run(box, "/get-involved", tmux="/tmp/fake,1,0")
    assert got.returncode == COULD_NOT_START, got.stderr
    assert "could not ask whether the plan has anything free" in got.stderr


def test_via_drain_is_not_accepted_yet(tmp_path):
    """#476 adds it, with a line here and a line in harness/README.md. Until then
    an unknown trigger is a caller nobody wrote."""
    got = run(sandbox(tmp_path, policy=INVOLVED_OK, explode=False),
              "--dry-run", "--via", "drain", "/get-involved")
    assert got.returncode != STARTED


# ------------------------------------- #563: the ceiling is a dial, the gate is not

#: A policy whose own ceiling is 2, so a dial that says something else is visibly
#: the layer that answered rather than a coincidence.
DIALLED = {"enabled": True, "commands": ["/fix-issue", "/panel-review-pr"],
           "max_sessions": 2}


def test_the_board_ceiling_answers_over_the_policy_file(tmp_path):
    """The whole of #563 in one assertion: raising 2 to 3 cost a nix edit, a build,
    a PR, a merge, a `nixos-rebuild` and a human with the password. Now it is a dial,
    and the file is what applies when nobody has set one."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 5)])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    answer = json.loads(got.stdout)
    assert answer["max_sessions"] == 5
    assert answer["max_sessions_policy"] == 2, (
        "the file's number is still reported — a caller has to be able to tell a "
        "box configured at 2 from a fleet dialled to 2")
    assert answer["max_sessions_source"] == "board"


def test_a_freeze_can_come_from_the_board_and_names_the_layer_that_froze_it(tmp_path):
    """`0` is the direction the issue says matters more: the only control that stops
    a box spawning without switching the mechanism off. The remedy has to name the
    DIAL — "raise max_sessions in spawn.json" sends somebody to edit a file whose
    number is being overridden, and on this fleet that edit is a build, a PR and a
    rebuild before they find out it changed nothing."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 0)])
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == AT_CAP, got.stderr
    assert "0 of 0" in got.stderr and "0 is a freeze" in got.stderr
    assert "spawn.max_sessions` on the board" in got.stderr
    assert "spawn.json" not in got.stderr, (
        "the file is not the remedy while a dial is overriding it")
    assert not any("qb-claim" in line for line in got.ran)


def test_the_file_is_the_remedy_when_the_file_is_what_answered(tmp_path):
    """The other half of the same sentence, so the message cannot simply always name
    the dial."""
    box = sandbox(tmp_path, policy={**DIALLED, "max_sessions": 0}, explode=False,
                  dials=[])
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == AT_CAP, got.stderr
    assert "spawn.json" in got.stderr and "on the board" not in got.stderr


def test_a_board_that_cannot_be_read_leaves_the_file_in_force_and_says_so(tmp_path):
    """FAILS OPEN, unlike its neighbours in that file and for `qb-admit`'s reason: it
    counts a resource rather than guarding a door. A ceiling that failed closed would
    stop a box over a board hiccup, which is worse than the thing it guards."""
    box = sandbox(tmp_path, policy={**DIALLED, "max_sessions": 0}, explode=False,
                  dials=None)
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == AT_CAP, got.stderr
    assert "the board's ceiling was not used" in got.stderr
    assert "the ceiling is" in got.stderr and "spawn.json's 0" in got.stderr


@pytest.mark.parametrize("value", [-1, True, "lots", None, 2.5])
def test_a_dial_that_is_not_a_number_of_sessions_is_refused_and_not_substituted(
        value, tmp_path):
    """`POST /dials` stores an opaque JSON value on purpose and
    `harness_rules.dial_problem` only checks it is a number, so `-1` and `true`
    arrive here having passed everything in front of them. A refused value leaves the
    ceiling where it was — on the file — rather than falling to a compiled-in number
    neither layer asked for."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", value)])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    answer = json.loads(got.stdout)
    assert answer["max_sessions"] == 2, (value, got.stderr)
    assert answer["max_sessions_source"] == "policy", value
    assert "not a non-negative whole number" in (answer["dials_problem"] or ""), \
        (value, answer)


def test_a_ceiling_an_agent_set_for_itself_is_refused(tmp_path):
    """The one dial an agent may not set, and since #591 the only thing enforcing
    that is here.

    `POST /dials` was human-only when this ceiling was put on the board, and the
    module docstring says that is what made it a throttle rather than a hole. The
    endpoint now also takes a delegated agent, so "an agent cannot raise its own
    ceiling" stopped being a property of the gate and had to become a property of
    the reader. An agent lifting its own cap from 2 to 40 is the self-approval
    shape #85, #86, #78, #232 and #335 each settled separately.
    """
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 40,
                              set_by="hermes/mist-harbour", set_via="agent")])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    answer = json.loads(got.stdout)
    assert answer["max_sessions"] == 2, (
        "the file's number must apply — an agent-set ceiling is not a ceiling")
    assert answer["max_sessions_source"] != "board"


def test_the_refused_agent_ceiling_says_who_set_it_and_what_to_do(tmp_path):
    """Refused NOISILY, for the reason every other ignored row here is: a spawner
    that quietly used the file's number leaves somebody looking at a board saying
    40 and a box behaving like 2, with nothing connecting the two."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 40,
                              set_by="hermes/mist-harbour", set_via="agent")])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    problem = json.loads(got.stdout)["dials_problem"] or ""
    assert "hermes/mist-harbour" in problem, problem
    assert "is not a person" in problem, problem
    assert "X-Human-Key" in problem, "the remedy has to name a door a person has"


def test_a_ceiling_a_person_set_with_a_key_is_still_honoured(tmp_path):
    """`agent` is the only refused value, not "anything that is not the edge". A
    person at a terminal with an `X-Human-Key` is a person — that is the whole
    point of the second method — and refusing `key` here would quietly retire the
    door #477 added."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 5, set_via="key")])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    assert json.loads(got.stdout)["max_sessions"] == 5


def test_a_row_older_than_the_set_via_column_is_honoured(tmp_path):
    """Null is "not recorded", never "some other method" — and a row with no
    `set_via` predates the column, which is to say predates an agent being able to
    write one at all. Treating absent as suspect would silently retire ceilings
    nobody can see are being ignored."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 5)])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    assert json.loads(got.stdout)["max_sessions"] == 5


def test_an_unknown_provenance_is_not_trusted_by_failing_to_match_agent(tmp_path):
    """An ALLOWLIST, not `!= "agent"`. A misspelling, a value from a newer board than
    this box, or a malformed row must not become person-authored by failing to match
    one string. The question is "do I know this was a person", not "is this the one
    bad value I have heard of"."""
    for via in ("agnt", "elevated", "", "something-new"):
        box = sandbox(tmp_path, policy=DIALLED, explode=False,
                      dials=[dial("spawn.max_sessions", 40, set_via=via)])
        got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
        answer = json.loads(got.stdout)
        assert answer["max_sessions"] == 2, f"{via!r} was trusted as a person"


def test_the_ceiling_is_asked_for_at_fleet_scope_and_bounded(tmp_path):
    """Two properties of the read itself. **No repo**, because a machine's
    concurrency is not a property of a repository — `live_spawns()` counts panes on
    this tmux server without knowing which checkout each is in, so there is no
    question a repo-scoped ceiling would have answered. And **bounded short**, for
    `harness_rules.DIALS_TIMEOUT`'s reason: a dashboard asks `--policy` on every
    click, so a board that is down must cost a moment rather than half a minute."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False, dials=[])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    reads = [r for r in got.reads if r["path"] == "/dials"]
    assert len(reads) == 1, f"the ceiling was read {len(reads)} times: {got.reads}"
    assert reads[0]["timeout"] == 5, reads[0]
    # The repo IS sent, and it buys the REPORT rather than the ceiling: `GET /dials`
    # answers a repo read with that repo's rows AND the fleet's, and only the fleet
    # rows can answer a ceiling counted on a tmux server.
    assert (reads[0]["params"] or {}).get("repo") == "acme/widget", reads[0]


def test_a_machine_that_never_opted_in_asks_the_board_nothing(tmp_path):
    """The property to break last, held against the new layer. A box with no policy
    file must reach no board, no token and no network — so the dial read sits AFTER
    the enabled check on both paths, and `explode` is left on to prove it."""
    box = sandbox(tmp_path)
    for argv in (("--policy", "--json"), ("/fix-issue", "277")):
        got = run(box, *argv, tmux="/tmp/fake,1,0")
        assert got.returncode == NOT_ENABLED, (argv, got.stderr)
        assert got.reads == [], f"{argv} asked the board {got.reads}"


def test_a_misuse_costs_no_board_call(tmp_path):
    """Every refusal above the ceiling is answerable from the file, and the dial is
    read at the first point the NUMBER is actually needed. A spawner that phoned home
    to tell somebody they had typed an unknown command would pay for the board on the
    one path that never needed it."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False, dials=[])
    assert run(box, "/rm-rf", "277").reads == []
    assert run(box, "/fix-issue", "not-a-number").reads == []
    assert run(box, "/fix-issue", "277", repo_path=str(tmp_path)).reads == []


# ---------------------------------------------- and the ceiling across the board

def test_the_fleet_ceiling_refuses_when_the_board_is_at_it(tmp_path):
    """The half that did not exist: `qb-pace` bounds the subscription's SPEND and
    `qb-admit` bounds ONE REPO's work, so five boxes each set to 3 had a ceiling of
    fifteen and nothing that knew it."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions_fleet", 4)], active=agents(4))
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == AT_FLEET_CAP, got.stderr
    assert "4 of 4 agent(s) are live across the whole board" in got.stderr
    assert "spawn.max_sessions_fleet" in got.stderr
    assert not any("qb-claim" in line for line in got.ran), (
        "the fleet gate is applied before the claim, like every other ceiling")


def test_its_own_exit_code_because_nothing_on_this_box_is_the_remedy(tmp_path):
    """Not a second flavour of `AT_CAP`. The codes exist so a caller can tell
    refusals apart by remedy, and these two share none: `AT_CAP` is answered here by
    closing a pane, and this one cannot be answered here at all."""
    assert AT_FLEET_CAP != AT_CAP
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions_fleet", 1)], active=agents(9))
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == AT_FLEET_CAP
    assert "nothing on this box moves that" in got.stderr


def test_room_across_the_board_starts_the_session(tmp_path):
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions_fleet", 10)], active=agents(3))
    got = run(box, "/fix-issue", "277", "--dry-run", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr


def test_a_fan_out_is_not_a_session(tmp_path):
    """`GET /active` returns `subagents` beside the agents and they are deliberately
    not counted: a fan-out holds no pane and no seat, and counting it would make a
    ceiling expressed in sessions bite on something that is not one."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions_fleet", 3)],
                  active=agents(2, subagents=40))
    got = run(box, "/fix-issue", "277", "--dry-run", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr


def test_with_no_fleet_dial_the_board_is_never_asked_to_count(tmp_path):
    """UNSET IS NO CEILING — how this fleet ran before the dial existed — and it
    costs no call, which is what keeps the ordinary spawn as cheap as it was."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False, dials=[])
    got = run(box, "/fix-issue", "277", "--dry-run", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    assert not [r for r in got.reads if r["path"] == "/active"], got.reads


def test_the_fleet_gate_fails_OPEN_and_is_the_only_one_that_does(tmp_path):
    """The gates that fail closed are counting a resource with the board as the only
    source of truth. This one has a local ceiling underneath it: a board outage
    degrades five boxes from "ten across the board" to "five each", which is bounded
    and nowhere near a runaway, while refusing on silence would stop every box on the
    fleet whenever the board hiccups."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions_fleet", 1)], active=None)
    got = run(box, "/fix-issue", "277", "--dry-run", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    assert "could not count the fleet's live agents" in got.stderr
    assert "this machine's own ceiling of 2" in got.stderr


def test_the_local_ceiling_is_applied_before_the_board_is_asked_to_count(tmp_path):
    """Order, and it is not cosmetic: the cheap local refusal must not pay for a
    board call it cannot be talked out of by."""
    box = sandbox(tmp_path, policy={**DIALLED, "max_sessions": 0}, explode=False,
                  dials=[dial("spawn.max_sessions_fleet", 10)], active=agents(0))
    got = run(box, "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == AT_CAP, got.stderr
    assert not [r for r in got.reads if r["path"] == "/active"], got.reads


def test_policy_reports_the_fleet_ceiling_so_a_button_can_see_a_freeze(tmp_path):
    """`--policy` is asked before a button is offered, and #371's rule is that a
    button which appears to work and does not is worse than one that is absent."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions_fleet", 7)])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    assert json.loads(got.stdout)["max_sessions_fleet"] == 7
    assert "7 live across the whole board" in got.stderr
    assert not [r for r in got.reads if r["path"] == "/active"], (
        "--policy reports the ceiling; counting against it is the spawn path's job")


def test_the_script_and_the_dial_registry_name_the_same_two_dials():
    """The no-second-source rule applied to this pair. A dial `qb-start` reads and
    `BOARD_DIALS` does not hold is `tempo`'s state — set, stored, reported as in
    force, and refused as unrecognised by every resolution on the fleet — which is
    the thing #563 exists to stop, not to reproduce."""
    sys.path.insert(0, str(BIN.parent / "loops"))
    import harness_rules as hr

    read = set(re.findall(r'^DIAL_[A-Z]+ = "([\w.]+)"', START.read_text(), re.M))
    assert read == {"spawn.max_sessions", "spawn.max_sessions_fleet"}
    for name in read:
        assert name in hr.BOARD_DIALS, f"{name} is read here and settable nowhere"
        assert hr.BOARD_DIALS[name].applies == "fleet", (
            f"{name} is not a path into any repo's rules")


def test_a_caller_on_a_ui_thread_can_ask_without_leaving_the_box(tmp_path):
    """`--policy`'s own promise is that a caller may ask it on every click without
    paying for it, and the dashboard asks it from the UI thread — where a board that
    is down would freeze the screen for `DIALS_TIMEOUT` on every keystroke. It gives
    up nothing by opting out: it reads only `enabled` and `commands`, both of which
    are the file's, and the ceiling it never consulted is still applied by the spawn
    one step later."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 5)])
    got = run(box, "--policy", "--no-board", "--json", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    assert got.reads == [], f"--no-board asked the board {got.reads}"
    answer = json.loads(got.stdout)
    assert answer["max_sessions"] == 2 and answer["max_sessions_source"] == "policy"


def test_the_spawn_path_cannot_opt_out_of_the_board_ceiling(tmp_path):
    """Refused rather than ignored. The ceiling is what decides whether this box may
    start anything at all, so a flag that appeared to switch it off would be a caller
    believing it had opted out of something it is still subject to — and a gate must
    never look like it took an instruction it did not."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 0)])
    got = run(box, "--no-board", "/fix-issue", "277", tmux="/tmp/fake,1,0")
    assert got.returncode == MISUSE, got.stderr
    assert "answers --policy and nothing else" in got.stderr
    assert got.reads == [] and got.ran == []


def test_the_dashboard_is_the_caller_that_takes_that_flag():
    """The coupling only this file can hold: `--no-board` exists for one caller, and
    a `--policy` on the UI thread that lost the flag is a five-second freeze per
    keystroke that nothing else would notice."""
    tui = BIN / "qb-dash-tui.py"
    assert '"--policy", "--no-board", "--json"' in tui.read_text(), (
        "the dashboard asks --policy from the UI thread and must not pay for a "
        "board call it does not read")


def test_a_ceiling_scoped_to_one_repo_is_named_rather_than_quietly_dropped(tmp_path):
    """The dashboard's picker refuses this write (`dial_scope_problem`), and the
    board takes it from anything else holding a human key — a `curl`, the web page —
    because `dial` is opaque text there and `repo` is just a column. A setting
    stored, reported as in force and read by nothing is the exact failure this layer
    exists to end, so the reader is the last place it can be said out loud."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 9, repo="acme/widget")])
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    answer = json.loads(got.stdout)
    assert answer["max_sessions"] == 2, "a repo-scoped ceiling must not apply"
    assert answer["max_sessions_source"] == "policy"
    assert "set on the board for acme/widget" in (answer["dials_problem"] or "")
    assert "Set it with no repo" in answer["dials_problem"]


def test_a_wrong_scoped_row_beside_a_good_one_does_not_name_the_file_as_in_force(
        tmp_path):
    """The half that is easy to get wrong: a refused row is reported AND the fleet
    row still answers, so the message must not go on to say the file's number is the
    ceiling. Naming the wrong layer as the one in force is the failure the whole
    provenance idea exists to prevent."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 9, repo="acme/widget"),
                         dial("spawn.max_sessions", 5)])
    got = run(box, "/fix-issue", "277", "--dry-run", "--json", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    answer = json.loads(got.stdout)
    assert answer["max_sessions"] == 5 and answer["max_sessions_source"] == "board"
    assert "set on the board for acme/widget" in got.stderr
    assert "the ceiling is" not in got.stderr, (
        "the file is not what answered, so it must not be named as the ceiling")


def test_a_checkout_that_cannot_name_itself_still_gets_a_ceiling(tmp_path):
    """The repo buys the report, not the number. A path with no slug asks for the
    fleet rows alone, which is the answer either way — it loses a diagnostic rather
    than a ceiling."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions", 5)])
    got = run(box, "--policy", "--json", "--repo-path", str(tmp_path),
              tmux="/tmp/fake,1,0")
    assert json.loads(got.stdout)["max_sessions"] == 5, got.stderr


@pytest.mark.parametrize("body,why", [
    ("list", "a JSON array — truthy, and with no `.get` on it"),
    ("dials-not-a-list", "an object whose `dials` is not a list"),
])
def test_a_board_answering_the_wrong_shape_is_a_ceiling_not_a_crash(body, why,
                                                                    tmp_path):
    """The shape this layer exists to survive. `body or {}` guards only the FALSY
    answers, and a JSON array is truthy — so `.get` on it is an AttributeError
    raised outside the fail-open try, which would crash the spawner over exactly the
    board trouble the fallback is for."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False, dials=body)
    got = run(box, "--policy", "--json", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, (why, got.stderr)
    answer = json.loads(got.stdout)
    assert answer["max_sessions"] == 2 and answer["max_sessions_source"] == "policy"
    assert "Traceback" not in got.stderr, (why, got.stderr)


def test_a_fleet_count_of_the_wrong_shape_fails_open_rather_than_raising(tmp_path):
    """Same guard on the other read, and here the crash would be worse: this gate's
    whole promise is that a board it cannot read leaves the local ceiling in charge
    rather than stopping the spawn."""
    box = sandbox(tmp_path, policy=DIALLED, explode=False,
                  dials=[dial("spawn.max_sessions_fleet", 1)], active="list")
    got = run(box, "/fix-issue", "277", "--dry-run", tmux="/tmp/fake,1,0")
    assert got.returncode == STARTED, got.stderr
    assert "could not count the fleet's live agents" in got.stderr
    assert "not an object" in got.stderr
    assert "Traceback" not in got.stderr

