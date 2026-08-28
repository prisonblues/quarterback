"""`create-worktree` asking whether there is room before it starts work (#337).

Rich, after watching eight agents fan out and fan back in over one morning: *"I
want rolling process not batch."* The costs were all of the predicted kind — two
branches minted migration `0029` independently (both authors ran `preflight`,
both were told GO, because that tool compares a branch against `main` and cannot
see an unlanded sibling), a third was renumbered twice mid-flight, and the
largest open diff went DIRTY the moment the first landed. Nothing counted:
`git worktree list` returned 48 on that box and this script had no notion that a
number existed.

**This is not new machinery. It is one refusal gaining a second reason.** The
checkout already refuses when the claim on *this issue* is held; with a ceiling
configured it also refuses when there is *no room*. Same moment — before
`git worktree add`, where a refusal costs nothing to unwind — and the same
three-answer shape the claim reads:

* room, or no bound configured -> proceed
* FULL -> refuse, always. The board is saying something definite, exactly as a
  409 on the claim is, and a bound enforced only when convenient is advice.
* cannot tell -> warn and proceed, unless `--require-claim`. Failing closed here
  would make the board a single point of failure for every checkout on the fleet.

The block is extracted from the real script rather than copied, so a refactor
that moves or renames it fails here instead of leaving this suite green about
code nobody runs — the rule `test_create_worktree_claim.py` follows.

Run: pytest harness/tests
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# A sibling module, imported by bare name — see `_path_sandbox`'s own docstring
# for why a suite that asserts a tool is absent cannot build its PATH from the
# host's.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "create-worktree"

BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not on PATH")

_START = "# >>> bound"
_END = "# <<< bound"


def bound_block() -> str:
    """The admission stanza, lifted out of create-worktree as it ships."""
    src = SCRIPT.read_text()
    assert _START in src and _END in src, (
        f"the {_START} / {_END} markers are gone from create-worktree, so this suite "
        "is asserting nothing — fix the markers rather than deleting the test")
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "admit_the_branch" in block, (
        "the markers no longer bracket the admission stanza")
    return block


PRELUDE = """
set -euo pipefail
RED=''; GREEN=''; YELLOW=''; CYAN=''; NC=''
die() { echo "Error: $1" >&2; exit 1; }
"""


def run_stanza(*, bound="true", claim="true", require="false",
               stub: str | None = None, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run `admit_the_branch` with a stub `qb-admit` of the caller's choosing.

    `stub=None` means no qb-admit anywhere — a real deployment state (a host with
    the harness half-installed) and one of the "cannot tell" cases.

    Run as a script FILE rather than `bash -c`, deliberately. The stanza falls
    back to `${0%/*}/qb-admit` when the tool is not on PATH, and under `-c` that
    `$0` is the interpreter's own path — so on a machine where the harness is
    installed beside bash, the "absent" case would find the real one.

    That guard was only half of it, and the missing half is what made
    `test_a_missing_qb_admit_does_not_abort_the_run_under_set_e` pass for four
    months without ever taking the branch it names (#472). PATH was `bindir` plus
    `dirname(bash)`, and on a home-manager install that second directory is the
    profile directory holding the real `qb-admit`: `command -v` found it before
    the `$0` fallback was ever reached, it ran against a throwaway repo, and it
    happened to satisfy `stderr == ""`. A test green for the wrong reason is
    worse than a red one, because nothing will ever tell you. `_path_sandbox`
    builds a PATH out of directories this test filled, so absent is absent.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if stub is not None:
        fake = bindir / "qb-admit"
        # Absolute bash, not `#!/usr/bin/env bash`: there is no /usr/bin/env
        # inside a nix build sandbox (#177).
        fake.write_text(f"#!{BASH}\n" + stub + "\n")
        fake.chmod(0o755)
    script = _path_sandbox.sibling_dir(tmp_path) / "stanza.sh"
    script.write_text(PRELUDE
                      + f'BOUND={bound}\nCLAIM={claim}\nREQUIRE_CLAIM={require}\n'
                      + f'MAIN_REPO={tmp_path}\n'
                      + bound_block()
                      + '\nadmit_the_branch\necho REACHED-THE-CHECKOUT\n')
    return subprocess.run(
        [BASH, str(script)], capture_output=True, text=True,
        env={"PATH": _path_sandbox.sandbox_path(tmp_path, bindir),
             "HOME": str(tmp_path)})


def reached(got) -> bool:
    return "REACHED-THE-CHECKOUT" in got.stdout


# ------------------------------------------------------------- what each answer costs

def test_room_proceeds_and_says_nothing_of_its_own(tmp_path):
    """qb-admit's own line is the report; the stanza adds none of its own when
    there is nothing wrong."""
    got = run_stanza(tmp_path=tmp_path, stub='echo "room: 3 of 5" >&2; exit 0')
    assert got.returncode == 0, got.stderr
    assert reached(got)
    assert "room: 3 of 5" in got.stderr


def test_a_full_window_refuses_the_checkout(tmp_path):
    """The one hard failure, and it refuses always rather than only under
    --require-claim: a full window is the board saying something definite, which
    is the same standing a held claim has."""
    got = run_stanza(tmp_path=tmp_path,
                     stub='echo "full: 5 of 5 in flight" >&2; exit 1')
    assert got.returncode != 0
    assert not reached(got), "a full window let the checkout through"
    assert "full" in got.stderr


def test_the_refusal_names_the_way_out(tmp_path):
    """A refusal that names no escape hatch leaves the next agent's remedy as
    editing the script — the rule the claim's own refusal already follows."""
    got = run_stanza(tmp_path=tmp_path, stub='exit 1')
    assert "--no-bound" in got.stderr
    assert "in_flight.max" in got.stderr


def test_the_refusal_says_the_work_stays_on_the_plan(tmp_path):
    """Admission, not queueing. The item is not started and is not lost: it stays
    unclaimed and visibly waiting, which is a state the plan already expresses."""
    got = run_stanza(tmp_path=tmp_path, stub='exit 1')
    assert "unclaimed" in got.stderr and "plan" in got.stderr


def test_an_unreadable_count_warns_and_proceeds(tmp_path):
    """A board outage must not stop every checkout on the fleet — the same policy
    the claim applies to its own "cannot tell", and for the same reason."""
    got = run_stanza(tmp_path=tmp_path,
                     stub='echo "unknown: board unreachable" >&2; exit 2')
    assert got.returncode == 0, got.stderr
    assert reached(got)
    assert "must not stop a checkout" in got.stderr


def test_require_claim_turns_an_unreadable_count_into_a_refusal(tmp_path):
    """`--require-claim` is the strict reading of the whole gate, not of the claim
    alone: a caller that will not start work it cannot register also will not
    start work it cannot count."""
    for stub, why in [('exit 2', "board down"), (None, "qb-admit absent")]:
        got = run_stanza(require="true", tmp_path=tmp_path, stub=stub)
        assert got.returncode != 0, f"{why} was allowed through --require-claim"
        assert not reached(got)
        assert "--require-claim" in got.stderr


def test_a_missing_qb_admit_does_not_abort_the_run_under_set_e(tmp_path):
    """`create-worktree` runs under `set -euo pipefail`, where an absent sibling
    script aborts the run it was meant to inform. It is also silent: a host that
    never installed this harness would otherwise get a warning on every checkout
    about a feature it has not got."""
    got = run_stanza(tmp_path=tmp_path, stub=None)
    assert got.returncode == 0, got.stderr
    assert reached(got)
    assert got.stderr == ""


# ------------------------------------------------------------------ the two opt-outs

def test_no_bound_skips_the_check_entirely(tmp_path):
    got = run_stanza(bound="false", tmp_path=tmp_path,
                     stub=f'touch {tmp_path}/called; exit 1')
    assert got.returncode == 0, got.stderr
    assert reached(got)
    assert not (tmp_path / "called").exists()


def test_no_claim_skips_it_too(tmp_path):
    """The count is claims. A checkout that takes none is outside the count either
    way, so refusing it would be bounding something this is not counting — which
    is Rich's own rule that work outside qb is outside qb. It is loud about being
    unclaimed (see `claim_the_branch`), which is what keeps the opt-out visible."""
    got = run_stanza(claim="false", tmp_path=tmp_path,
                     stub=f'touch {tmp_path}/called; exit 1')
    assert got.returncode == 0, got.stderr
    assert not (tmp_path / "called").exists()


def test_no_bound_still_takes_the_claim(tmp_path):
    """The override waives the REFUSAL, not the registration: a checkout taken
    over the ceiling still counts, so the window reports the truth about itself
    rather than the truth minus whoever overrode it."""
    src = SCRIPT.read_text()
    assert "BOUND=false" in src
    block = src.split(_START, 1)[1].split(_END, 1)[0]
    assert "CLAIM=false" not in block, (
        "--no-bound must not also switch the claim off")


# --------------------------------------------------------------- where it is wired in

def test_the_window_is_asked_before_the_claim_is_taken(tmp_path):
    """Being refused for lack of room must not leave a claim taken. A claim taken
    and then rolled back is a slot that flickered — visible to a peer counting at
    the wrong moment, and a `renewed` answer this script would decline to release."""
    src = SCRIPT.read_text()
    assert src.index("\nadmit_the_branch\n") < src.index('\nclaim_the_branch "$BRANCH_NAME"')


def test_both_run_before_the_worktree_exists(tmp_path):
    """A refusal has then cost nothing, and the loser has a directory-free repo
    rather than a half-built worktree to clean up."""
    src = SCRIPT.read_text()
    assert src.index("\nadmit_the_branch\n") < src.index('step "Creating git worktree')


@pytest.mark.parametrize("flag", ["--no-bound"])
def test_the_flag_is_documented_in_the_usage_header(flag):
    """The header is the only documentation `create-worktree` has (there is no
    --help), so a flag missing from it is a flag nobody finds."""
    src = SCRIPT.read_text()
    header = src.split("# ============================================", 1)[0]
    assert flag in header, f"{flag} is not in the usage header"
