"""Something has to run the flake checks (#179).

Workflow-shape assertions, in the manner of ``test_announce_workflow.py``, plus the
behavioural tests that drive the workflow's own discovery script. The property that
broke is not textual: ``flake.nix`` declared four checks and no workflow referenced the
flake at all, so they ran when a human typed ``nix build`` and at no other time. That is
how ``worktree-tests`` stayed red on ``main`` for a day with eight release-number
assertions erroring inside it (#163).

The behavioural tests are the ones that matter most. ``nix flake check`` on a flake whose
checks have been removed exits 0 — so the obvious job, the one that only runs that
command, reports green the day the checks disappear. The discovery step exists to make
that loud, and a guard nobody exercises is the thing this repo keeps re-learning.

Where this module runs, and why nothing here is guarded against a nix sandbox: it lives
in the top-level ``tests/`` tree, which ``pyproject.toml`` names in ``testpaths`` and the
``app suite`` job runs with ``uv run --extra dev pytest`` against an ordinary checkout.
No check in ``flake.nix`` copies ``tests/`` into its sandbox — ``loops-tests`` takes
``harness/loops``, ``worktree-tests`` takes ``harness/bin`` and ``harness/tests``,
``mcp-tests`` takes ``mcp/``, and ``harness-build`` builds a package. So ``import yaml``
and the reads of ``.github/`` and ``flake.nix`` below are reads of a full working tree,
which is the only environment this file is ever collected in. Guarding them with a
``skipif`` would convert "the workflow file vanished" into a green report, which is the
one outcome this module exists to prevent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# The checks that must not quietly disappear. Deliberately a floor and not an exact
# set: adding a check should cost nobody an edit here (#163 added one and will add
# more), but removing or renaming one has to be a visible line in a diff rather than a
# count that silently drops by one. Gutting a check — keeping the name, swapping the
# body for `pkgs.emptyFile` — is not detectable at name level by this or by the
# workflow's own count guard, and the workflow comment says so rather than implying
# cover it does not give.
EXPECTED_CHECKS = frozenset({"harness-build", "loops-tests", "worktree-tests", "mcp-tests"})

# The discovery step is bash and jq. Both are present on ubuntu-latest, which is where
# the `app suite` job that collects this file runs, so in CI these tests do not skip —
# and test_the_behavioural_tests_are_not_skipped_in_ci below is what proves it rather
# than assumes it. One marker rather than the same decorator written twice, so the
# condition and the reason cannot drift apart.
_MISSING_TOOLS = tuple(tool for tool in ("jq", "bash") if not shutil.which(tool))
needs_jq_and_bash = pytest.mark.skipif(
    bool(_MISSING_TOOLS),
    reason=f"the discovery step is bash and jq; missing: {', '.join(_MISSING_TOOLS)}",
)

# A system string no hardcoded literal could match by accident. The stub `nix` reports
# this as `builtins.currentSystem` and then refuses any flakeref that does not name it,
# so a discovery script that went back to a written-down `x86_64-linux` fails here
# instead of passing — the bug that has already landed twice (#148, #176).
STUB_SYSTEM = "stub64-quarterback"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    """The workflow's ``on:`` block.

    PyYAML is a YAML 1.1 parser, in which a bare ``on`` is a boolean, so the key of the
    trigger block deserializes as ``True`` and not as the string ``"on"``. Reaching for
    ``workflow["on"]`` raises KeyError on a file that is perfectly correct.
    ``test_announce_workflow.py`` hits the same thing.
    """
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), (
        "tests.yml has no `on:` block, or it is not a mapping — note that PyYAML "
        f"resolves the bare key `on` to the boolean True, and this file's keys are "
        f"{sorted(map(str, workflow))}")
    return triggers


def _runs(step: dict) -> str:
    """The commands a step actually runs, with comment lines dropped.

    Selecting a step by a substring of its whole `run:` block matches the phrase in a
    comment or an echo just as happily as a command, so a job whose only mention of
    `nix flake check` was in a comment would satisfy the assertions below without ever
    running it.
    """
    lines = str(step.get("run", "")).splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _flake_job() -> dict:
    """The job that runs the flake, found by what it does rather than by its name."""
    jobs = _workflow("tests.yml")["jobs"]
    running = [job for job in jobs.values()
               if any("nix flake check" in _runs(step) for step in job.get("steps", []))]
    assert running, (
        "no job in tests.yml runs `nix flake check`, so flake.nix's checks run only when "
        "somebody types `nix build` by hand — the state #179 was filed about")
    assert len(running) == 1, (
        f"{len(running)} jobs run the flake; all but one of them is doing it twice over")
    return running[0]


def _flake_check_step(job: dict) -> dict:
    steps = [step for step in job.get("steps", []) if "nix flake check" in _runs(step)]
    assert len(steps) == 1, f"expected one step running the flake, found {len(steps)}"
    return steps[0]


def _discovery_step(job: dict) -> dict:
    """The step that names the checks, which is not the step that runs them."""
    steps = [step for step in job.get("steps", [])
             if "checks" in str(step.get("name", "")).lower()
             and "nix flake check" not in _runs(step)]
    assert steps, "the flake job has no step that names the checks before running them"
    assert len(steps) == 1, (
        f"{len(steps)} steps in the flake job look like the discovery step "
        f"({', '.join(str(step.get('name')) for step in steps)}); the behavioural tests "
        "below would silently exercise whichever came first")
    assert "run" in steps[0], (
        f"the discovery step {steps[0].get('name')!r} has no `run:` — if it became a "
        "`uses:` the behavioural tests below have nothing left to drive")
    return steps[0]


def _nix_install_step(job: dict) -> dict:
    steps = [step for step in job.get("steps", [])
             if "install-nix-action" in str(step.get("uses", ""))]
    assert len(steps) == 1, f"expected one install-nix-action step, found {len(steps)}"
    return steps[0]


def _declared_checks() -> set[str]:
    """The check names flake.nix declares, read out of the `checks = forAllSystems` block."""
    source = (REPO_ROOT / "flake.nix").read_text(encoding="utf-8")
    match = re.search(r"^      checks = forAllSystems \(pkgs: \{\n(.*?)^      \}\);$",
                      source, re.MULTILINE | re.DOTALL)
    assert match, (
        "could not find the `checks = forAllSystems (pkgs: {` block in flake.nix — it "
        "was reformatted or restructured, and this test's parser has to follow it "
        "rather than be deleted")
    return set(re.findall(r"^        ([A-Za-z0-9_-]+) = ", match.group(1), re.MULTILINE))


def test_a_job_runs_the_flake_checks():
    # `_flake_job` is what enforces this: it raises when no job runs the command and
    # when more than one does. Asserting on its result rather than merely calling it
    # keeps the property in the body of the test that names it.
    step = _flake_check_step(_flake_job())
    assert "nix flake check" in _runs(step)


def test_the_flake_check_asks_for_the_build_log():
    """Without -L a red check reports `builder for '...drv' failed` and the assertion
    that actually broke is behind a `nix log` on a machine nobody has."""
    run = _runs(_flake_check_step(_flake_job()))
    assert re.search(r"nix flake check\b[^\n]*(\s-L\b|\s--print-build-logs\b)", run), (
        f"`nix flake check` is run without -L: {run.strip()!r}")


def test_the_checks_are_named_before_they_are_run():
    """Discovery after the check would report the empty set only once the job had
    already gone green on a flake that declares nothing."""
    steps = _flake_job()["steps"]
    assert steps.index(_discovery_step(_flake_job())) < steps.index(
        _flake_check_step(_flake_job())), "the checks are named after they are run"


def test_nix_is_installed_with_the_features_every_step_here_needs():
    config = str(_nix_install_step(_flake_job()).get("with", {}).get("extra_nix_config", ""))
    assert "nix-command" in config and "flakes" in config, (
        "install-nix-action no longer enables nix-command and flakes, without which "
        f"`nix eval` and `nix flake check` both refuse to run: {config!r}")


def test_the_third_party_nix_action_is_pinned_to_a_commit():
    """A mutable tag on a third-party action that runs before any code of ours, holding
    the job's token, is repointable by someone who is not a reviewer here."""
    uses = str(_nix_install_step(_flake_job())["uses"])
    ref = uses.partition("@")[2]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"cachix/install-nix-action is pinned to {ref!r} rather than a full commit SHA")


def test_the_flake_job_carries_a_sane_timeout():
    """Presence is not the property: `timeout-minutes: 0` and a six-hour value both
    satisfy `in job` and neither one catches a wedged run."""
    timeout = _flake_job().get("timeout-minutes")
    assert isinstance(timeout, int) and not isinstance(timeout, bool), (
        f"the flake job's timeout-minutes is {timeout!r}, which GitHub will not honour")
    assert 1 <= timeout <= 30, (
        f"timeout-minutes is {timeout}; the documented intent is that a run still going "
        "is wedged rather than slow, and this value no longer expresses that")


def test_the_flake_job_reports_on_pull_requests_too():
    """The `stamped` job below it is main-only by design and must NOT be a required
    check. This one is the opposite: a flake that only breaks after the merge is a
    consumer's problem to discover, which is the arrangement #179 replaced."""
    assert "if" not in _flake_job(), (
        "the flake job has become conditional — if it no longer runs on pull_request it "
        "cannot gate anything, and requiring it would hang every PR waiting for a run "
        "that by design never happens")
    # The job-level `if:` is only half of it. Deleting `pull_request` from the
    # workflow's `on:` block, or fencing it behind a `paths` filter, makes the job
    # push-only just as effectively while leaving the assertion above green.
    triggers = _triggers(_workflow("tests.yml"))
    assert "pull_request" in triggers, (
        f"tests.yml no longer runs on pull_request (triggers: {sorted(map(str, triggers))}), "
        "so the flake job cannot gate a merge and requiring it would hang every PR")
    filters = triggers["pull_request"] or {}
    assert not (set(filters) & {"paths", "paths-ignore"}), (
        f"tests.yml's pull_request trigger has a path filter ({filters}); a required "
        "check that does not report on every PR leaves those PRs pending forever")


def test_the_expected_flake_checks_are_still_declared():
    """The workflow's count guard catches the checks being emptied. It cannot catch one
    of them being dropped while others are added, because the count survives that."""
    declared = _declared_checks()
    assert declared, "parsed flake.nix's checks block and found no check names in it"
    missing = EXPECTED_CHECKS - declared
    assert not missing, (
        f"flake.nix no longer declares {sorted(missing)} (it declares {sorted(declared)}). "
        "If that is deliberate, remove them from EXPECTED_CHECKS in the same commit — "
        "the point is that a check leaving is a line in a diff rather than a silent "
        "drop in a count nobody reads")


def test_the_behavioural_tests_are_not_skipped_in_ci():
    """A skip nobody sees is a failure wearing a green badge — flake.nix installs tmux
    for exactly this reason. The two tests below are the only ones that execute the
    discovery script rather than read it, so a runner that quietly lost jq would leave
    the guard untested and the summary green."""
    if not os.environ.get("CI"):
        pytest.skip("not CI; a developer without jq installed is not a regression")
    assert not _MISSING_TOOLS, (
        f"CI is missing {', '.join(_MISSING_TOOLS)}, so the discovery step's own tests "
        "skipped rather than ran — install them on the runner rather than accepting a "
        "green report from a guard that was never exercised")


def _write_stub_nix(stub_dir: Path) -> None:
    """A stubbed `nix` that checks what it was asked, not merely that it was asked.

    Written as bash with an absolute interpreter path. Not ``/usr/bin/env``: a runtime
    stub whose shebang cannot be resolved is the failure that cost three suites a day
    between them (#177). Not this interpreter either — ``sys.executable`` under nix is a
    store path with a version suffix, and a shebang line has a hard kernel limit and no
    quoting, so the length and any space in it are asserted here rather than surfacing
    later as an unexplained exec failure.
    """
    bash = shutil.which("bash")
    assert bash, "bash disappeared between the skipif above and here"
    shebang = f"#!{bash}"
    assert " " not in bash, f"bash's path contains a space, which a shebang cannot quote: {bash!r}"
    assert len(shebang.encode()) < 128, (
        f"the shebang line is {len(shebang.encode())} bytes, over the kernel's limit: {shebang!r}")

    nix = stub_dir / "nix"
    nix.write_text(
        f"{shebang}\n"
        "set -u\n"
        # `nix eval --impure --raw --expr 'builtins.currentSystem'`
        "case \" $* \" in\n"
        "  *currentSystem*) printf '%s' \"$STUB_SYSTEM\"; exit 0 ;;\n"
        "esac\n"
        # Anything else is the checks query, and it must name the system we just
        # reported. A script that went back to a written-down system fails here.
        "if [ \"${2-}\" != \".#checks.$STUB_SYSTEM\" ]; then\n"
        "  printf 'stub nix: asked for %s, but the current system is %s\\n' \\\n"
        "    \"${2-<nothing>}\" \"$STUB_SYSTEM\" >&2\n"
        "  exit 3\n"
        "fi\n"
        "if [ -n \"${STUB_CHECKS_MISSING-}\" ]; then\n"
        "  printf \"error: attribute 'checks' missing\\n\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "printf '%s\\n' \"$STUB_NAMES\"\n",
        encoding="utf-8",
    )
    nix.chmod(0o755)


def _run_discovery(tmp_path: Path, names_json: str = "[]", *,
                   checks_missing: bool = False,
                   script: str | None = None) -> subprocess.CompletedProcess:
    """Run the workflow's own discovery script against a stubbed `nix`."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(parents=True)
    _write_stub_nix(stub_dir)
    # `os.environ.get`, not `os.environ["PATH"]`: an environment with no PATH is odd but
    # possible, and it should not surface as a KeyError from inside a helper.
    path = f"{stub_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}"
    env = dict(os.environ, PATH=path, STUB_SYSTEM=STUB_SYSTEM, STUB_NAMES=names_json)
    if checks_missing:
        env["STUB_CHECKS_MISSING"] = "1"
    if script is None:
        script = _discovery_step(_flake_job())["run"]
    # A timeout because the stub not being picked up — a PATH surprise, an exec-format
    # error — means this runs the real `nix`, and a suite that hangs reports nothing.
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=env, timeout=120, check=False)


@needs_jq_and_bash
def test_the_discovery_step_fails_loudly_when_the_flake_declares_no_checks(tmp_path):
    proc = _run_discovery(tmp_path, "[]")
    assert proc.returncode != 0, (
        "the flake declared no checks and the job carried on — which is exactly the green "
        "report #179 exists to prevent, since `nix flake check` itself exits 0 here")
    assert "::error::" in proc.stdout, "the failure has to be annotated, not just non-zero"


@needs_jq_and_bash
def test_the_discovery_step_fails_loudly_when_the_checks_output_is_gone(tmp_path):
    """The other way for the checks to disappear: deleted from flake.nix outright rather
    than emptied. Then `nix eval` is what fails, before any count can be taken, and an
    uncaught failure would end the job on a raw nix error that reads as a broken flake
    rather than as the checks having been removed."""
    proc = _run_discovery(tmp_path, checks_missing=True)
    assert proc.returncode != 0
    assert "::error::" in proc.stdout, (
        "`checks` was removed from the flake entirely and the step failed without an "
        f"annotation, so the reason is buried in nix's own stderr: {proc.stderr!r}")


@needs_jq_and_bash
def test_the_discovery_step_names_the_checks_it_found(tmp_path):
    proc = _run_discovery(tmp_path, '["worktree-tests","mcp-tests"]')
    assert proc.returncode == 0, proc.stderr
    # Named in the log, not merely counted: this is how a reviewer sees that a check
    # added by a PR is actually being run rather than assumed to be.
    assert "worktree-tests" in proc.stdout and "mcp-tests" in proc.stdout
    # The count, without pinning the printf's exact wording or its pluralisation — the
    # names above carry the real signal and a tidy-up of the message is not a regression.
    assert re.search(r"\b2\b[^\n]*check", proc.stdout), proc.stdout


@needs_jq_and_bash
def test_the_discovery_step_derives_the_system_rather_than_naming_one(tmp_path):
    """The stub refuses any flakeref that does not name the system it reported, so this
    is the assertion that a written-down `x86_64-linux` cannot pass — the bug that has
    landed twice already (#148, #176). Running the real script proves the guard is armed;
    running a mutated copy proves it would fire."""
    good = _run_discovery(tmp_path / "real", '["worktree-tests"]')
    assert good.returncode == 0, good.stderr

    hardcoded = _discovery_step(_flake_job())["run"].replace(
        '".#checks.$system"', '".#checks.x86_64-linux"')
    assert '".#checks.x86_64-linux"' in hardcoded, (
        "the discovery script no longer queries `.#checks.$system`, so this test's "
        "mutation no longer produces the bug it exists to catch")
    bad = _run_discovery(tmp_path / "mutated", '["worktree-tests"]', script=hardcoded)
    assert bad.returncode != 0, (
        "a discovery script naming a hardcoded system passed against a runner reporting "
        f"a different one: {bad.stdout!r}")
