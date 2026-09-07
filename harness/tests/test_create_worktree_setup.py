"""`create-worktree`'s `setup` hook — commands that build the worktree's own environment.

`symlinks` and `copies` were the only two levers a project had, and neither
*builds* anything. That is why a Python project's worktrees all symlinked one
`.venv`: there was no way to ask for their own. A shared venv is a single mutable
dependency set behind N branches, so whichever worktree last ran `uv sync`
decides what all of them have installed — measured in lexray at 44 worktrees
sharing one venv across 4 distinct `uv.lock` files, where a sync from one branch
removed four packages the others import.

Three properties are pinned here, and each is a way the hook could look like it
works while not working:

1. **Commands run in the WORKTREE**, not in the main checkout or the caller's cwd.
   A `uv sync` run in the wrong directory builds the wrong project's venv, which
   is the exact failure the hook exists to remove.
2. **A failure does not abort the script.** The worktree already exists by this
   point; aborting would lose the branch and the port along with the mistake, and
   skip the port/nginx/summary steps that still need to run. Under
   `set -euo pipefail` that is not the default — it has to be written.
3. **A failure is recorded, not swallowed.** An absent `.venv` otherwise surfaces
   much later as a pre-push refusal or an import error with no obvious cause, so
   the command has to survive to the closing summary.

The block is extracted from the real script rather than copied here, so a
refactor that moves or renames it fails in this suite instead of leaving it green
about code nobody runs any more — the rule `test_create_worktree_db_name.py` and
`test_create_worktree_rerere.py` both follow.

Run: pytest harness/tests
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "create-worktree"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")

_START = "# >>> setup"
_END = "# <<< setup"


def setup_block() -> str:
    """The setup-command loop, lifted out of create-worktree as it ships."""
    src = SCRIPT.read_text()
    assert _START in src and _END in src, (
        f"the {_START} / {_END} markers are gone from create-worktree, so this suite "
        "is asserting nothing — fix the markers rather than deleting the test"
    )
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "SETUP_FAILED" in block, "the markers no longer bracket the setup loop"
    return block


def run_block(tmp_path: Path, commands: list[str]) -> subprocess.CompletedProcess:
    """Run the stanza with a chosen command list, under the script's own shell flags."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)

    array = " ".join(f'"{c}"' for c in commands)
    script = f"""
set -euo pipefail
RED=''; GREEN=''; YELLOW=''; NC=''
step() {{ :; }}
WORKTREE_DIR={worktree}
SETUP_COMMANDS=({array})
{setup_block()}
echo "REACHED_END"
printf 'FAILED:%s\\n' "${{SETUP_FAILED[@]:-}}"
"""
    # Deliberately run FROM a different directory: that is what makes "the
    # commands ran in the worktree" a real assertion rather than an accident of cwd.
    return subprocess.run(
        ["bash", "-c", script],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestCommandsRunInTheWorktree:
    def test_command_runs_with_the_worktree_as_cwd(self, tmp_path):
        result = run_block(tmp_path, ["pwd"])
        assert str(tmp_path / "worktree") in result.stdout, result.stdout
        assert "elsewhere" not in result.stdout

    def test_a_command_that_writes_lands_in_the_worktree(self, tmp_path):
        run_block(tmp_path, ["touch built-here"])
        assert (tmp_path / "worktree" / "built-here").exists()
        assert not (tmp_path / "elsewhere" / "built-here").exists()

    def test_a_cd_does_not_leak_into_the_next_command(self, tmp_path):
        """Each command gets a subshell, or command N relocates command N+1."""
        result = run_block(tmp_path, ["cd /tmp", "pwd"])
        assert str(tmp_path / "worktree") in result.stdout, result.stdout


class TestFailureIsLoudButNotFatal:
    def test_a_failing_command_does_not_abort_the_script(self, tmp_path):
        """`set -e` would kill the run here, skipping port, nginx and the summary."""
        result = run_block(tmp_path, ["false"])
        assert result.returncode == 0, result.stderr
        assert "REACHED_END" in result.stdout

    def test_a_failing_command_is_recorded_for_the_summary(self, tmp_path):
        result = run_block(tmp_path, ["false"])
        assert "FAILED:false" in result.stdout, result.stdout

    def test_the_failure_names_the_command_and_the_remedy(self, tmp_path):
        result = run_block(tmp_path, ["false"])
        assert "FAILED: false" in result.stdout
        assert "re-run it yourself" in result.stdout
        assert str(tmp_path / "worktree") in result.stdout

    def test_later_commands_still_run_after_one_fails(self, tmp_path):
        run_block(tmp_path, ["false", "touch ran-anyway"])
        assert (tmp_path / "worktree" / "ran-anyway").exists()

    def test_success_records_no_failure(self, tmp_path):
        result = run_block(tmp_path, ["true"])
        assert "FAILED:\n" in result.stdout or "FAILED:" in result.stdout
        assert "FAILED: true" not in result.stdout


class TestNoCommandsIsANoOp:
    def test_empty_list_runs_nothing_and_still_defines_the_array(self, tmp_path):
        """`SETUP_FAILED` is read by the summary on every path, so it must exist."""
        result = run_block(tmp_path, [])
        assert result.returncode == 0, result.stderr
        assert "REACHED_END" in result.stdout


class TestDocumentedWhereItIsUsed:
    def test_the_example_config_shows_the_key(self):
        example = SCRIPT.resolve().parents[1] / "worktree.example.json"
        assert '"setup"' in example.read_text(), (
            "worktree.example.json is the annotated one-file reference for every key; "
            "a key absent from it is a key nobody discovers"
        )

    def test_the_readme_lists_it_among_the_arrays(self):
        readme = SCRIPT.resolve().parents[1] / "README.md"
        text = readme.read_text()
        assert "`setup`" in text
        assert "the arrays `symlinks`, `copies`, `setup`" in text, (
            "the README's key list is what a reader greps; `setup` must be in it"
        )
