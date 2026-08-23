"""A deploy that silently does not happen is a release that silently does not ship (#392).

The `deploy` job's webhook curl timed out at its 30s cap on two of the last
twenty-five runs on main, each with successes either side. Nothing retried and
nothing said so anywhere a person looks, so the image sat in GHCR with the edge
never told to pull it while every other signal on the release was green.

Most of these are *behavioural* rather than shape assertions, in the manner of
``test_flake_workflow.py``: the step's own ``run:`` script is extracted from the
workflow and executed under bash with a stubbed ``curl`` and ``sleep``, so what is
under test is what the runner will actually do. A shape assertion cannot tell a
retry loop that fails loudly from one that ends in ``|| true``, and that
distinction is the whole point of the change — a loop that swallows the exit code
turns an occasional silent miss into a permanent one.

Same place and the same reasoning as ``test_flake_workflow.py`` and
``test_harness_job.py``: this lives in the top-level ``tests/`` tree that
``pyproject.toml`` names in ``testpaths`` and the ``app suite`` job runs against an
ordinary checkout, so reading ``.github/`` here is always a read of a full working
tree and a ``skipif`` around it would turn "the workflow file vanished" into a
green report.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Values no real log line could contain by accident, so "the secret did not leak"
# is a search for a string that is only ever in the environment.
STUB_URL = "https://edge.invalid-quarterback-test/webhooks/SECRET-PATH-SEGMENT-392"
STUB_TOKEN = "SECRET-DEPLOY-TOKEN-392"

# Two markers rather than one, because they guard different steps: the webhook
# step is bash and nothing else, and only the board report shells out to jq. A
# single `bash and jq` marker would skip every retry test on a machine that
# merely lacked jq — which is the deploy half of this module, the half the issue
# is about, silently not running while the summary said green.
needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="the deploy step is bash")
needs_bash_and_jq = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("jq")),
    reason="the board report builds its body with jq",
)
_MISSING_TOOLS = tuple(tool for tool in ("bash", "jq") if not shutil.which(tool))


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _runs(step: dict) -> str:
    """A step's commands with comment lines dropped.

    Selecting or asserting on a substring of a whole ``run:`` block matches the
    phrase in a comment as happily as in a command — and this job's script is
    mostly comments, so ``"|| true" in step["run"]`` would be satisfied by a
    comment explaining why there is no ``|| true``.
    """
    lines = str(step.get("run", "")).splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _deploy_job() -> dict:
    return _workflow("docker-build.yml")["jobs"]["deploy"]


def _step_named(fragment: str) -> dict:
    """A step of the deploy job, found by a fragment of its name."""
    steps = [s for s in _deploy_job()["steps"] if fragment in str(s.get("name", ""))]
    assert len(steps) == 1, (
        f"expected exactly one deploy step whose name contains {fragment!r}, found "
        f"{[s.get('name') for s in steps]} — the behavioural tests below would "
        "otherwise silently exercise whichever came first")
    return steps[0]


def _webhook_step() -> dict:
    return _step_named("Trigger deploy")


def _board_step() -> dict:
    return _step_named("on the board")


# --------------------------------------------------------------------------
# Running the real script
# --------------------------------------------------------------------------

def _write_stub_curl(stub_dir: Path) -> None:
    """A ``curl`` that fails for the first ``STUB_FAILURES`` calls, then succeeds.

    It records each invocation's arguments to ``$STUB_CURL_LOG`` so a test can
    assert on what the step asked for, and it writes the ``-w`` format's worth of
    output on *failure* as well as success — real curl does, and a stub that
    stayed silent would let a script that only reads timings on the happy path
    pass here and print an empty diagnosis in production.

    An absolute interpreter in the shebang, not ``/usr/bin/env``: a runtime stub
    whose shebang cannot be resolved is a failure that has already cost this repo
    a day (#177).
    """
    bash = shutil.which("bash")
    assert bash, "bash disappeared between the skipif and here"
    assert " " not in bash, f"bash's path contains a space, which a shebang cannot quote: {bash!r}"

    curl = stub_dir / "curl"
    curl.write_text(
        f"#!{bash}\n"
        "set -u\n"
        'printf "%s\\n" "$*" >> "$STUB_CURL_LOG"\n'
        'calls=$(wc -l < "$STUB_CURL_LOG")\n'
        # The board step also shells out to curl; it is told to succeed by
        # STUB_FAILURES being reset for that run, so one stub serves both.
        'if [ "$calls" -le "${STUB_FAILURES:-0}" ]; then\n'
        '  printf "HTTP 000 in 30.000000s (dns 0.001s, tcp 0.000s, tls 0.000s)"\n'
        '  exit "${STUB_EXIT:-28}"\n'
        "fi\n"
        'printf "HTTP 204 in 0.043000s (dns 0.004s, tcp 0.011s, tls 0.030s)"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)


def _write_stub_sleep(stub_dir: Path) -> None:
    """A ``sleep`` that records instead of waiting.

    Without it the backoff test spends fifteen real seconds proving arithmetic,
    and — more to the point — a suite cannot tell "slept 5s" from "slept 500s",
    which is the difference between a retry and a wedged job.
    """
    bash = shutil.which("bash")
    assert bash
    sleeper = stub_dir / "sleep"
    sleeper.write_text(
        f"#!{bash}\n"
        "set -u\n"
        'printf "%s\\n" "${1-}" >> "$STUB_SLEEP_LOG"\n',
        encoding="utf-8",
    )
    sleeper.chmod(0o755)


def _run_step(
    step: dict,
    tmp_path: Path,
    *,
    failures: int = 0,
    exit_code: int = 28,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, dict[str, Path]]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    _write_stub_curl(stub_dir)
    _write_stub_sleep(stub_dir)

    logs = {
        "curl": tmp_path / "curl.log",
        "sleep": tmp_path / "sleep.log",
        "github_env": tmp_path / "github_env",
        "summary": tmp_path / "step_summary.md",
    }
    for path in logs.values():
        path.touch()

    full = dict(
        os.environ,
        PATH=f"{stub_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
        STUB_CURL_LOG=str(logs["curl"]),
        STUB_SLEEP_LOG=str(logs["sleep"]),
        STUB_FAILURES=str(failures),
        STUB_EXIT=str(exit_code),
        GITHUB_ENV=str(logs["github_env"]),
        GITHUB_STEP_SUMMARY=str(logs["summary"]),
    )
    full.update(env or {})
    # A timeout because a stub that was not picked up means the real curl runs
    # against a real edge, and a suite that hangs reports nothing.
    proc = subprocess.run(
        ["bash", "-c", str(step["run"])],
        capture_output=True, text=True, env=full, timeout=120, check=False,
    )
    return proc, logs


def _deploy_env(**overrides: str) -> dict[str, str]:
    env = {"DEPLOY_WEBHOOK_URL": STUB_URL, "DEPLOY_TOKEN": STUB_TOKEN}
    env.update(overrides)
    return env


# --------------------------------------------------------------------------
# The retry itself
# --------------------------------------------------------------------------

@needs_bash
def test_a_healthy_webhook_is_called_once(tmp_path):
    proc, logs = _run_step(_webhook_step(), tmp_path, failures=0, env=_deploy_env())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert logs["curl"].read_text().count("\n") == 1, (
        "the happy path called curl more than once — the retry is meant to cost "
        f"nothing when the first attempt lands: {logs['curl'].read_text()!r}")
    assert logs["sleep"].read_text() == "", "the happy path slept"


@needs_bash
def test_one_timeout_no_longer_loses_the_deploy(tmp_path):
    """The defect, stated as a test: before the retry, this run shipped nothing."""
    proc, logs = _run_step(_webhook_step(), tmp_path, failures=1, env=_deploy_env())
    assert proc.returncode == 0, (
        "a single timeout still lost the deploy: " + proc.stdout + proc.stderr)
    assert logs["curl"].read_text().count("\n") == 2
    assert "attempt 1/3 failed" in proc.stdout, (
        f"the retry happened but said nothing about it: {proc.stdout!r}")
    assert "curl exit 28 (timed out at the 30s cap)" in proc.stdout, (
        "the log names no reason for the failed attempt, so the next person to read "
        f"it learns only that something went wrong: {proc.stdout!r}")


@needs_bash
def test_two_timeouts_still_leave_a_third_attempt(tmp_path):
    proc, logs = _run_step(_webhook_step(), tmp_path, failures=2, env=_deploy_env())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert logs["curl"].read_text().count("\n") == 3


@needs_bash
def test_an_exhausted_retry_still_fails_the_job(tmp_path):
    """The property that makes the retry safe rather than a mask.

    A loop that ends in ``|| true`` converts an occasional silent failure into a
    permanent one: every deploy would then report green whether or not the edge
    was ever told anything.
    """
    proc, logs = _run_step(_webhook_step(), tmp_path, failures=99, env=_deploy_env())
    assert proc.returncode != 0, (
        "three failed attempts and the step exited 0 — the retry is now hiding the "
        f"outage it was added to survive: {proc.stdout!r}")
    assert logs["curl"].read_text().count("\n") == 3, (
        f"expected exactly three attempts: {logs['curl'].read_text()!r}")


@needs_bash
def test_the_backoff_is_short_enough_that_a_real_outage_stays_cheap(tmp_path):
    """An unreachable edge must cost a red job, not an ever-growing wait — the
    reason ``--max-time`` was introduced in the first place."""
    proc, logs = _run_step(_webhook_step(), tmp_path, failures=99, env=_deploy_env())
    assert proc.returncode != 0
    waits = [int(line) for line in logs["sleep"].read_text().split()]
    assert waits, "the attempts were made back to back, with no backoff at all"
    assert len(waits) == 2, f"three attempts should sleep twice, slept {len(waits)}: {waits}"
    assert sum(waits) <= 30, (
        f"the backoff totals {sum(waits)}s; with three 30s attempts the job now spends "
        "longer failing than the two minutes the --max-time cap exists to prevent")


@needs_bash
def test_the_cap_on_each_attempt_is_still_thirty_seconds(tmp_path):
    """Not a shape assertion on the text: what curl was *invoked* with.

    Raising the cap is the obvious wrong fix — the successful runs take 0-2s and
    the failures take the full cap with nothing in between, so a bigger number
    buys waiting rather than deploys.
    """
    _, logs = _run_step(_webhook_step(), tmp_path, failures=99, env=_deploy_env())
    for line in logs["curl"].read_text().splitlines():
        assert "--max-time 30" in line, f"an attempt ran without the 30s cap: {line!r}"


@needs_bash
def test_every_attempt_is_the_whole_request_and_not_a_truncated_one(tmp_path):
    """The stub answers by call count, so on its own it would let a retry loop
    that had lost the Authorization header, the body or the URL pass here.

    Asserting on the recorded invocation is what makes the count mean something:
    three POSTs that reach nothing, or three unauthenticated ones, are not three
    attempts at the deploy.
    """
    _, logs = _run_step(_webhook_step(), tmp_path, failures=99, env=_deploy_env())
    invocations = logs["curl"].read_text().splitlines()
    assert len(invocations) == 3
    for line in invocations:
        assert STUB_URL in line, f"an attempt was sent to no URL at all: {line!r}"
        assert f"Authorization: {STUB_TOKEN}" in line, (
            f"an attempt carried no deploy token, so the edge would reject it: {line!r}")
        assert "-X POST" in line, f"an attempt was not a POST: {line!r}"
        assert "Content-Type: application/json" in line, line
        assert "-d {}" in line, f"an attempt sent no body: {line!r}"


@needs_bash
def test_an_unconfigured_webhook_still_skips_rather_than_fails(tmp_path):
    """A fork has no secret, and the build half must still pass there."""
    proc, logs = _run_step(
        _webhook_step(), tmp_path, failures=99,
        env=_deploy_env(DEPLOY_WEBHOOK_URL="", DEPLOY_TOKEN=""),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert logs["curl"].read_text() == "", "an unconfigured deploy called the webhook anyway"


# --------------------------------------------------------------------------
# Legibility: the reason has to survive to somewhere a person is
# --------------------------------------------------------------------------

@needs_bash
def test_a_dead_deploy_annotates_the_run_rather_than_only_exiting_one(tmp_path):
    proc, logs = _run_step(_webhook_step(), tmp_path, failures=99, env=_deploy_env())
    assert "::error title=Deploy webhook never fired::" in proc.stdout, (
        "the failure is non-zero and unannotated, so the Actions UI shows a red X "
        f"whose reason is inside a collapsed log group: {proc.stdout!r}")
    annotations = [ln for ln in proc.stdout.splitlines() if ln.startswith("::error")]
    assert len(annotations) == 1
    assert "GHCR" in annotations[0], (
        "the annotation does not say the image exists, which is the fact that decides "
        f"whether re-running the job is enough: {annotations[0]!r}")

    summary = logs["summary"].read_text()
    assert "Deploy webhook never fired" in summary, (
        f"nothing was written to the run summary: {summary!r}")
    assert "curl exit 28" in summary, (
        f"the run summary records no diagnosis: {summary!r}")


@needs_bash
def test_the_diagnosis_reaches_the_board_step(tmp_path):
    """The board step runs in a fresh shell, so anything it says about *why* has
    to be handed over through the environment file rather than a variable."""
    _, logs = _run_step(_webhook_step(), tmp_path, failures=99, env=_deploy_env())
    handed = dict(
        line.split("=", 1) for line in logs["github_env"].read_text().splitlines() if "=" in line
    )
    assert handed.get("DEPLOY_CURL_EXIT") == "28"
    assert "timed out" in handed.get("DEPLOY_CURL_REASON", "")
    assert "tcp" in handed.get("DEPLOY_CURL_TIMINGS", ""), (
        "no per-phase timings were carried over, and those are the numbers that say "
        f"whether the connection was ever established: {handed!r}")
    for name, value in handed.items():
        assert "\n" not in value, f"{name} is multi-line, which GITHUB_ENV cannot carry"


@needs_bash_and_jq
def test_the_failed_deploy_is_posted_to_the_board(tmp_path):
    step = _board_step()
    proc, logs = _run_step(
        step, tmp_path, failures=0,
        env={
            "QUARTERBACK_TOKEN": "board-token-392",
            "QUARTERBACK_BASE_URL": "https://board.invalid/",
            "COMMIT_SHA": "39a0413fdf7814cdba2861a7527c22f651a6a2f4",
            "BRANCH": "main",
            "REPO": "prisonblues/quarterback",
            "RUN_URL": "https://github.com/prisonblues/quarterback/actions/runs/1",
            "DEPLOY_CURL_EXIT": "28",
            "DEPLOY_CURL_REASON": "timed out at the 30s cap",
            "DEPLOY_CURL_TIMINGS": "HTTP 000 in 30.000000s (dns 0.001s, tcp 0.000s, tls 0.000s)",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    called = logs["curl"].read_text()
    assert "https://board.invalid/post" in called, (
        "the board step posted somewhere other than /post, or the trailing slash was "
        f"not stripped: {called!r}")
    assert '"type":"stuck"' in called, (
        "the post is not one of the types the human board renders, so it lands where "
        f"nobody sees it — the failure this step exists to end: {called!r}")
    assert "39a0413" in called and "GHCR" in called


@needs_bash_and_jq
def test_an_unconfigured_board_skips_rather_than_masking_the_deploy_failure(tmp_path):
    proc, logs = _run_step(
        _board_step(), tmp_path, failures=0,
        env={"QUARTERBACK_TOKEN": "", "QUARTERBACK_BASE_URL": "",
             "COMMIT_SHA": "0" * 40, "BRANCH": "main", "REPO": "a/b", "RUN_URL": "x"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert logs["curl"].read_text() == ""


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

@needs_bash
@pytest.mark.parametrize("failures", [0, 1, 99])
def test_neither_the_webhook_url_nor_the_token_is_ever_printed(tmp_path, failures):
    """Actions masks a secret only where the whole value appears in a line, so
    curl's own ``Could not resolve host: <edge>`` would reach the log unmasked.
    CI logs are readable and the URL's path segment is itself the capability."""
    proc, logs = _run_step(_webhook_step(), tmp_path, failures=failures, env=_deploy_env())
    printed = proc.stdout + proc.stderr + logs["summary"].read_text() \
        + logs["github_env"].read_text()
    for secret in ("SECRET-PATH-SEGMENT-392", "SECRET-DEPLOY-TOKEN-392",
                   "edge.invalid-quarterback-test"):
        assert secret not in printed, (
            f"{secret!r} reached the log, the run summary or GITHUB_ENV, all of which "
            "are readable by anyone who can read the run")


def test_the_step_does_not_trace_its_own_commands():
    """``set -x`` would echo the curl invocation, token header and all, whatever
    the assertions above say about the messages the script writes itself."""
    for step in (_webhook_step(), _board_step()):
        run = _runs(step)
        assert "set -x" not in run, f"{step['name']!r} traces, which prints the token"
        assert "-euxo" not in run and "-uxo" not in run, f"{step['name']!r} traces"


# --------------------------------------------------------------------------
# Shape: things a stub run cannot observe
# --------------------------------------------------------------------------

def test_the_deploy_failure_cannot_be_swallowed_by_the_job():
    """``continue-on-error`` on the webhook step would make every deploy green.

    Not reachable from the stub run: it is the runner, not the script, that acts
    on the flag.
    """
    step = _webhook_step()
    assert step.get("continue-on-error") is not True, (
        "the deploy step is continue-on-error, so a dead edge now reports green — "
        "the silent failure #392 was filed about, made permanent")


def test_the_board_report_runs_only_when_the_deploy_failed():
    step = _board_step()
    assert str(step.get("if", "")).strip() == "failure()", (
        f"the board report's condition is {step.get('if')!r}; `failure()` is what keeps "
        "it from posting a `stuck` on every green release")
    assert step.get("continue-on-error") is not True, (
        "the board report is continue-on-error — the job it runs in has already "
        "failed, so the flag can only hide a rotated token")


def test_the_board_report_uses_the_same_mechanism_as_the_announce_job():
    """Not a second way for CI to talk to the board (#392 asked for one, not two)."""
    announce = _workflow("announce-board.yml")["jobs"]["announce"]["steps"][0]
    report = _runs(_board_step())
    assert "QUARTERBACK_BASE_URL%/}/post" in report, (
        "the board report posts somewhere other than the endpoint `announce` uses")
    assert "Authorization: Bearer $QUARTERBACK_TOKEN" in report
    for guard in ("QUARTERBACK_TOKEN", "QUARTERBACK_BASE_URL"):
        assert guard in _runs(announce) and guard in report


def test_the_deploy_job_still_does_not_announce_the_commit():
    """#127's property, kept: the announce must not be reintroduced here.

    Narrowed from "no step in `deploy` mentions QUARTERBACK_TOKEN" — which #392's
    board report legitimately does — to what that assertion was standing in for.
    Two sources of `published` would double-post every commit; a `stuck` posted
    only when the rollout did not happen is not a second announcement, it is the
    thing nothing was saying.
    """
    steps = _deploy_job()["steps"]
    assert not any('"published"' in _runs(step) or "published" in str(step.get("name", "")).lower()
                   for step in steps), (
        "the deploy job posts a `published` again — b86ff0b is the commit that "
        "proves announcing must not be contingent on an image having built")


def test_the_behavioural_tests_are_not_skipped_in_ci():
    """A skip nobody sees is a failure wearing a green badge.

    Everything above that actually runs the workflow's script is guarded on bash
    and jq being present. On a runner that quietly lost one of them the module
    would report green having executed nothing, which is precisely the shape of
    failure #392 is about.
    """
    if not os.environ.get("CI"):
        pytest.skip("not CI; a developer without jq installed is not a regression")
    assert not _MISSING_TOOLS, (
        f"CI is missing {', '.join(_MISSING_TOOLS)}, so the behavioural tests here "
        "skipped rather than ran — install them on the runner rather than accepting a "
        "green report from a guard that was never exercised")


def test_the_deploy_step_carries_no_shell_option_that_hides_a_failure():
    run = _runs(_webhook_step())
    assert "set -euo pipefail" in run, (
        "the deploy script no longer aborts on an unexpected error, so a typo in the "
        "retry loop would read as a successful deploy")
