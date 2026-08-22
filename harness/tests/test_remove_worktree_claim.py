"""`remove-worktree` handing back what `create-worktree` took (#337).

The checkout claim is taken by a script, on behalf of a worktree, before the
agent that will use the tree exists — so it names no session, belongs to the
machine, and runs for 8h. #277's `stop` half releases a *session's* claims and
therefore cannot reach it; nothing on the landing path touches it either. On
2026-08-22 four plan items still carried live claims after their PRs had merged,
one of them shipped as v2.78 hours earlier, and the only thing that would have
freed those four slots was the TTL expiring in the evening.

Teardown is the mechanical half of the fix: symmetry with the thing that
acquired it, and it catches abandoned work as well as landed work. `/drop-worktree`
is a driver over this script, so wiring it here covers that too.

Three properties worth naming:

* it releases what the CREATE-NAME says, never the worktree's resolved branch.
  They diverge when somebody checks out something else inside the tree, and it is
  the create-name that `create-worktree` derived its claim from — so releasing
  what `RESOLVED_BRANCH` names would hand back a claim this worktree never took,
  which on a box where another agent holds it is worse than leaving it held.
* it releases nothing while the worktree directory is still on disk. Step 4
  reports "Done" whether or not the delete succeeded, and a claim handed back
  over a tree somebody can still work in is a slot reported free that is taken.
* it never changes the outcome of the teardown. A coordination tool that cannot
  be reached must not leave a worktree half-removed — but it does record a
  cleanup warning, because a claim silently left held for eight hours is exactly
  the state this exists to stop, and "Cleanup complete" printed over it is how it
  went unnoticed for a day.

The block is extracted from the real script rather than copied, so a refactor
that moves or renames it fails here rather than leaving this suite green about
code nobody runs.

Run: pytest harness/tests
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "remove-worktree"

BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not on PATH")

_START = "# >>> release"
_END = "# <<< release"


def release_block() -> str:
    src = SCRIPT.read_text()
    assert _START in src and _END in src, (
        f"the {_START} / {_END} markers are gone from remove-worktree, so this suite "
        "is asserting nothing — fix the markers rather than deleting the test")
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "release_the_claim" in block, (
        "the markers no longer bracket the release stanza")
    return block


#: The colours and the warning collector the stanza uses, which live further up
#: the real script — and `set +e`, which is what that script actually runs under
#: ("Don't exit on errors — try all cleanup steps").
PRELUDE = """
set +e
RED=''; GREEN=''; YELLOW=''; NC=''
CLEANUP_WARNINGS=()
warn_cleanup() { CLEANUP_WARNINGS+=("$1"); echo "WARNED: $1"; }
"""


def run_stanza(branch="feat/issue-337", *, keep="false", stub: str | None = None,
               strict: bool = False, dir_left: bool = False,
               tmp_path: Path) -> subprocess.CompletedProcess:
    """Run `release_the_claim` with a stub `qb-release` of the caller's choosing.

    Run as a script FILE rather than `bash -c`: the stanza falls back to
    `${0%/*}/qb-release` when the tool is not on PATH, and under `-c` that `$0`
    is the interpreter's own path — so on a machine with the harness installed
    beside bash, `stub=None` would find the real one.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if stub is not None:
        fake = bindir / "qb-release"
        fake.write_text(f"#!{BASH}\n" + stub + "\n")
        fake.chmod(0o755)
    left = ""
    if dir_left:
        left = str(tmp_path / "still-here")
        Path(left).mkdir(exist_ok=True)
    script = tmp_path / "stanza.sh"
    script.write_text(("set -euo pipefail\n" if strict else "")
                      + PRELUDE
                      + f'KEEP_CLAIM={keep}\nMAIN_REPO={tmp_path}\n'
                      + release_block()
                      + f'\nrelease_the_claim {branch!r} {left!r}\n'
                      + 'echo "WARNINGS=${#CLEANUP_WARNINGS[@]}"\n')
    return subprocess.run(
        [BASH, str(script)], capture_output=True, text=True,
        env={"PATH": f"{bindir}:{os.path.dirname(BASH)}", "HOME": str(tmp_path)})


def warnings(got) -> int:
    line = next(ln for ln in got.stdout.splitlines() if ln.startswith("WARNINGS="))
    return int(line.split("=", 1)[1])


# ----------------------------------------------------------------- what it asks for

def test_it_releases_the_branch_it_was_given_and_names_the_main_repo(tmp_path):
    """By BRANCH, so `qb-release` re-derives the issue and the board derives the
    key: the number is never typed a second time (#172). Keyed on the MAIN repo,
    because by this point the worktree's own directory is gone."""
    got = run_stanza("feat/issue-337", tmp_path=tmp_path,
                     stub=f'printf "%s\\n" "$@" >{tmp_path}/argv; exit 0')
    assert got.returncode == 0, got.stderr
    argv = (tmp_path / "argv").read_text().split("\n")
    assert argv[argv.index("--branch") + 1] == "feat/issue-337"
    assert argv[argv.index("--repo-path") + 1] == str(tmp_path)


def test_it_is_the_create_name_that_is_released_not_the_resolved_branch(tmp_path):
    """The call site, in the real script. A worktree created for one issue and
    later switched to another branch still holds the claim `create-worktree` took
    from the create-name; releasing `RESOLVED_BRANCH` would hand back somebody
    else's."""
    src = SCRIPT.read_text()
    assert 'release_the_claim "$BRANCH_NAME" "$WORKTREE_DIR"' in src
    assert 'release_the_claim "$RESOLVED_BRANCH"' not in src


def test_it_runs_last_after_the_tree_is_actually_gone(tmp_path):
    """A claim handed back while the checkout still exists is a slot reported free
    that somebody is standing in."""
    src = SCRIPT.read_text()
    assert src.index("release_the_claim \"$BRANCH_NAME\"") > src.index("STEP 6: Delete git branch")
    assert src.index("release_the_claim \"$BRANCH_NAME\"") < src.index("# SUMMARY")


def test_the_step_counter_was_bumped_with_the_step(tmp_path):
    """Adding a step without the total prints `[8/7]`, which reads as a bug in the
    teardown at exactly the moment somebody is watching one."""
    src = SCRIPT.read_text()
    assert "TOTAL_STEPS=8" in src


# ------------------------------------------------------- what each answer costs

def test_a_clean_release_says_done_and_warns_about_nothing(tmp_path):
    got = run_stanza(tmp_path=tmp_path, stub='echo "released: work/x#337" >&2; exit 0')
    assert got.returncode == 0
    assert "Done" in got.stdout
    assert warnings(got) == 0


def test_a_branch_naming_no_issue_is_not_a_warning(tmp_path):
    """`qb-release` answers 0 for "nothing to hand back", which is what most
    branches are. A teardown that warned about every one of them would train the
    reader to skip the summary that matters."""
    got = run_stanza("chore/tidy", tmp_path=tmp_path,
                     stub='echo "nothing: names no issue" >&2; exit 0')
    assert warnings(got) == 0


def test_somebody_elses_claim_is_left_alone_without_a_warning(tmp_path):
    """Exit 1 is the ownership rule working: another session on this box holds it,
    and this teardown is not the party to hand it back. Nothing is broken, so
    nothing is warned about — it is reported and left."""
    got = run_stanza(tmp_path=tmp_path, stub='echo "refused: zeus has it" >&2; exit 1')
    assert got.returncode == 0
    assert "Not ours" in got.stdout
    assert warnings(got) == 0


def test_a_board_that_could_not_be_reached_IS_a_cleanup_warning(tmp_path):
    """The failure this issue is about, reported rather than swallowed: the claim
    stays held for the rest of its TTL, and the old unconditional "Cleanup
    complete" over that is how five orphan databases accumulated unnoticed once
    already."""
    got = run_stanza(tmp_path=tmp_path, stub='echo "unknown: board down" >&2; exit 2')
    assert got.returncode == 0
    assert warnings(got) == 1
    assert "stays held" in got.stdout


def test_a_worktree_that_is_still_there_keeps_its_claim(tmp_path):
    """The teardown prints "Done" at step 4 whether or not the directory actually
    went: the recursive delete is silenced and fails against root-owned files a
    container wrote, which is the case `prune-worktrees --remove-dirs` carries a
    sudo hint for. Handing the claim back over a directory somebody can still
    `cd` into reports a slot free that is occupied — precisely the wrong answer
    for a window that counts claims."""
    got = run_stanza(dir_left=True, tmp_path=tmp_path,
                     stub=f'touch {tmp_path}/called; exit 0')
    assert got.returncode == 0, got.stderr
    assert not (tmp_path / "called").exists(), (
        "released the claim on a worktree that is still on disk")
    assert warnings(got) == 1, "left the claim held and said nothing about it"
    assert "still present" in got.stdout


def test_keep_claim_skips_it_and_says_so(tmp_path):
    """For a teardown that is not the end of the work — re-creating the tree, or
    carrying on in the main checkout. Silence here would look like the release
    happened."""
    got = run_stanza(keep="true", tmp_path=tmp_path,
                     stub=f'touch {tmp_path}/called; exit 0')
    assert got.returncode == 0
    assert not (tmp_path / "called").exists()
    assert "--keep-claim" in got.stdout
    assert warnings(got) == 0


def test_a_missing_qb_release_is_said_and_not_fatal(tmp_path):
    """A host with the board tooling absent still gets its worktree removed."""
    got = run_stanza(tmp_path=tmp_path, stub=None)
    assert got.returncode == 0, got.stderr
    assert "qb-release not found" in got.stdout
    assert warnings(got) == 0


def test_it_survives_set_e_as_well_as_the_scripts_own_set_plus_e(tmp_path):
    """remove-worktree runs under `set +e` today. That is a property of the file
    around this stanza, not of the stanza, and a teardown step that aborts the run
    the day somebody tightens the file is a bad thing to leave lying about."""
    got = run_stanza(tmp_path=tmp_path, strict=True, stub='exit 2')
    assert got.returncode == 0, got.stderr
    assert warnings(got) == 1


@pytest.mark.parametrize("flag", ["--keep-claim"])
def test_the_flag_is_documented_in_the_usage_header(flag):
    src = SCRIPT.read_text()
    header = src.split("# ============================================", 1)[0]
    assert flag in header, f"{flag} is not in the usage header"
