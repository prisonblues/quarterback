"""The `harness suites` job must get cheaper without getting quieter (#233).

That job used to run every suite under ``harness/`` twice — once plain, once with the
``tui`` extra — and then run the two dashboard modules a third time to prove they had not
skipped. The second full pass bought nothing: outside the dashboard modules no harness
test behaves differently for having Textual and rich installed, so a minute of every push
went on re-running a 220-line bash suite and a real tmux server to learn the same thing
twice.

The repair narrows the ``tui`` pass to the modules that actually need ``tui``. That is the
dangerous kind of edit here, because the cheapest way to make any test job faster is to
stop running things, and a job that runs less reports exactly the same green as one that
runs everything (#169). So the narrowing is allowed to stand only while it is *true*, and
this module is what makes it true rather than assumed:

* :func:`test_the_tui_pass_names_every_module_that_needs_textual_or_rich` goes red the day
  a new harness test imports ``textual`` or ``rich`` and nobody adds it to the step.
  Without it that module would skip in the plain pass, never run in the narrowed one, and
  report green by never executing — the exact defect the ``tui`` pass was added to end.
* :func:`test_the_tui_pass_proves_each_named_module_actually_ran` keeps the ``N passed``
  grep, because a module that skips its whole body still exits 0.
* :func:`test_the_plain_pass_still_discovers_every_suite` keeps the narrowing off pass 1,
  which is the run that proves the harness imports neither package.

Same place and the same reasoning as ``test_flake_workflow.py``: the top-level ``tests/``
tree, which ``pyproject.toml`` names in ``testpaths`` and the ``app suite`` job runs
against an ordinary checkout — so reading ``.github/`` and ``harness/`` here is always a
read of a full working tree, and a ``skipif`` guarding those reads would turn "the
workflow file vanished" into a green report.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"

#: A reference to Textual or rich *as a package*, not as an English word. The docstring of
#: ``harness/tests/test_qbdata.py`` says "wants textual and a configured board", which a
#: bare substring search reads as a dependency and this does not. What it does catch is
#: every shape the two dashboard modules use — a plain import, the deferred import inside
#: ``_why_no_tui``, ``pytest.importorskip`` and ``find_spec`` — because those are the
#: shapes a skip guard is written in.
NEEDS_TUI = re.compile(
    r"^[ \t]*(?:import|from)[ \t]+(?:textual|rich)\b"
    r"|importorskip\([ \t]*[\"'](?:textual|rich)[\"']"
    r"|find_spec\([ \t]*[\"'](?:textual|rich)[\"']",
    re.MULTILINE,
)

#: The `harness/.../test_*.py` paths a step names literally.
NAMED_MODULE = re.compile(r"harness/\S*?test_\w+\.py")

PLAIN_STEP = "Run every harness suite"
TUI_STEP = "Run the dashboard suites with the dashboard's dependencies"


def _harness_steps() -> dict[str, str]:
    """The harness job's steps, by name, as the shell each one runs."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return {
        step["name"]: step.get("run", "")
        for step in workflow["jobs"]["harness"]["steps"]
        if "name" in step
    }


def _harness_test_modules() -> list[Path]:
    """Every test module the harness job's own discovery would collect."""
    modules = [
        path
        for suite in sorted(REPO_ROOT.glob("harness/**/tests"))
        if suite.is_dir() and "node_modules" not in suite.parts
        for path in sorted(suite.glob("test_*.py"))
    ]
    assert modules, (
        "no harness test modules were found at all — they moved, and every assertion "
        "below would now pass by having nothing left to check"
    )
    return modules


def _modules_named_by(step_body: str) -> set[str]:
    return set(NAMED_MODULE.findall(step_body))


def test_the_harness_job_still_has_both_passes():
    steps = _harness_steps()
    assert PLAIN_STEP in steps, (
        f"the harness job no longer has a step named {PLAIN_STEP!r}. If it was renamed, "
        "rename it here too; if it was deleted, the harness suites now run nowhere."
    )
    assert TUI_STEP in steps, (
        f"the harness job no longer has a step named {TUI_STEP!r} — the dashboard tests "
        "skip without the tui extra, so dropping that step makes all of them green by "
        "never running."
    )


def test_the_plain_pass_still_discovers_every_suite():
    """Pass 1 stays wide. It is the run that proves the harness needs no Textual."""
    body = _harness_steps()[PLAIN_STEP]
    assert "find harness -type d -name tests" in body, (
        "the plain pass no longer discovers its suites. A hardcoded list is how a suite "
        "moves and stops running with nothing going red."
    )
    assert "No test suites found under harness/" in body, (
        "the plain pass lost its empty-discovery guard — a `for` loop over an empty array "
        "is a step that runs nothing and reports green."
    )
    assert not _modules_named_by(body), (
        "the plain pass now names individual modules. It must run whole suites: it is the "
        "only pass most harness tests get, and narrowing it would drop them silently."
    )


def test_the_plain_pass_can_run_the_parallelism_it_asks_for():
    """`-n auto` needs pytest-xdist installed, and `--extra dev` is what installs it.

    Not a style point. pytest rejects an unknown option before it collects anything, so
    the flag and the plugin drifting apart is a step that dies on `unrecognized
    arguments: -n` having run zero tests — loud, but a good deal less clear than this.
    """
    body = _harness_steps()[PLAIN_STEP]
    if not re.search(r"pytest\b[^\n]*[ \t]-n[ \t]", body):
        return
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "pytest-xdist" in pyproject, (
        "the harness job runs pytest with `-n`, but pyproject.toml no longer declares "
        "pytest-xdist — so `uv run --extra dev pytest -n auto` exits on an unrecognised "
        "argument before collecting a single test."
    )


def test_the_tui_pass_is_narrowed_to_the_modules_that_need_it():
    """The point of #233: the tui pass must not re-run every suite."""
    body = _harness_steps()[TUI_STEP]
    assert "find harness -type d -name tests" not in body, (
        "the tui pass discovers every suite again. That is the minute per push #233 "
        "removed: nothing outside the dashboard modules behaves differently for having "
        "Textual installed."
    )
    assert _modules_named_by(body), (
        "the tui pass names no module at all, so it either runs nothing or runs everything."
    )


def test_the_tui_pass_names_every_module_that_needs_textual_or_rich():
    """The guard that lets the narrowing stand.

    Add a harness test that imports Textual or rich, forget this step, and the module
    skips in pass 1, never runs in pass 2, and reports green by never executing. That is
    #169's defect class with a stopwatch as the motive, so it is checked and not trusted.
    """
    named = _modules_named_by(_harness_steps()[TUI_STEP])
    needs = {
        str(path.relative_to(REPO_ROOT))
        for path in _harness_test_modules()
        if NEEDS_TUI.search(path.read_text(encoding="utf-8"))
    }
    assert needs, (
        "no harness test module references textual or rich any more. If the dashboard "
        "suites were deleted, delete the tui pass with them; if they were rewritten, the "
        "detector above no longer sees them and the narrowing is unguarded."
    )
    missing = sorted(needs - named)
    assert not missing, (
        "these harness test modules need the tui extra, and the narrowed tui pass does "
        f"not name them — so they skip in pass 1 and never run at all: {missing}. Add "
        f"them to the {TUI_STEP!r} step in .github/workflows/tests.yml."
    )


def test_every_module_the_tui_pass_names_exists():
    """A rename must be red here, not a step quietly running one file instead of two."""
    missing = sorted(
        name
        for name in _modules_named_by(_harness_steps()[TUI_STEP])
        if not (REPO_ROOT / name).is_file()
    )
    assert not missing, (
        f"the tui pass names files that do not exist: {missing}. They were renamed or "
        "moved, and the step now runs fewer modules than it reads as running."
    )


def test_the_tui_pass_proves_each_named_module_actually_ran():
    """A module that skips its whole body exits 0, so green is not evidence on its own."""
    body = _harness_steps()[TUI_STEP]
    assert "[0-9]+ passed" in body, (
        "the tui pass lost its `N passed` guard. test_qb_dash.py skips its entire module "
        "without textual and still exits 0 — which is how those tests went green in CI "
        "for months without ever running."
    )
    assert "::error::" in body, (
        "the tui pass can no longer annotate the failure it exists to catch, so a skipped "
        "dashboard suite would be a green step with a note nobody reads."
    )
    assert "--extra tui" in body, (
        "the tui pass no longer installs the tui extra, which is the only reason the "
        "dashboard modules run at all."
    )


def test_the_tui_pass_checks_each_named_module_separately():
    """One run over both would report `N passed` while one contributed nothing.

    ``test_qb_dash.py`` is skip-guarded on textual and ``test_qb_dials_surface.py`` on rich.
    Collected together, either one skipping in full is invisible: the other's passes
    satisfy the grep.
    """
    body = _harness_steps()[TUI_STEP]
    # `[\s\\]` and not `\s`: the list is long enough to wrap, and a backslash-newline
    # between two suites still names them separately. Matching only literal whitespace
    # made this red for a reflow that changed nothing about what the step runs.
    assert re.search(r"for\s+\w+\s+in\s+harness/\S+\.py[\s\\]+harness/\S+\.py", body), (
        "the tui pass no longer iterates its modules one at a time. Both dashboard "
        "modules in a single pytest run means one of them can skip entirely and the "
        "`N passed` guard still passes on the other one's tests."
    )


@pytest.mark.parametrize(
    "source, expected",
    [
        ("import textual\n", True),
        ("from rich.table import Table\n", True),
        ('pytest.importorskip("rich", reason="the printed renderer needs rich")\n', True),
        ('importlib.util.find_spec("textual") is None\n', True),
        ("    import rich, textual  # noqa: F401\n", True),
        ('"""wants textual and a configured board."""\n', False),
        ("assert qd.scope_of('/home/rich/lexray') == 'lexray'\n", False),
        ("richness = 1\nimport textualise\n", False),
    ],
)
def test_the_detector_reads_dependencies_and_not_english(source, expected):
    """The guard above is only as good as this regex, so the regex is tested directly."""
    assert bool(NEEDS_TUI.search(source)) is expected
