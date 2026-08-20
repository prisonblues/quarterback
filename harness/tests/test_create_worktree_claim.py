"""`create-worktree` taking the claim at checkout — #172's structured pickup.

The issue's finding is that the claim primitive had never once been written to by
anything automatic: the lifecycle hook takes a session *lease*, `preland` reads a
merge claim nothing ever wrote, and the documented workflow told agents that
claiming meant posting a `status`. Thirteen agents, three shared checkouts, one
claim — and that one written by hand because a human told an agent to.

So the write has to hang off an action that already happens, and the checkout is
the one. **The unit of work exists before the agent does**: an issue number is
already in the branch name, so nothing is asked and nothing is opted into.

Three properties, and the third is the one worth arguing about:

* the issue number is DERIVED from the branch, never passed. A `--issue` flag is a
  second place the number is written, and an agent that types the wrong one claims
  somebody else's issue while working its own — worse than not claiming, because
  the record reads as authoritative.
* a claim HELD BY SOMEBODY ELSE refuses the checkout, always. That is the board
  saying something definite.
* a claim that cannot be TAKEN — no board, no token, board down, or a branch that
  names no issue — warns and proceeds. Failing closed there would make the board a
  single point of failure for every worktree on the fleet, and `--require-claim`
  is how a caller asks for the strict reading instead.

The block is extracted from the real script rather than copied, so a refactor that
moves or renames it fails here instead of leaving this suite green about code
nobody runs — the rule `test_create_worktree_rerere.py` and
`test_create_worktree_db_name.py` both follow.

Run: pytest harness/tests
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "create-worktree"

BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not on PATH")

_START = "# >>> claim"
_END = "# <<< claim"


def claim_block() -> str:
    """The claim stanza, lifted out of create-worktree as it ships."""
    src = SCRIPT.read_text()
    assert _START in src and _END in src, (
        f"the {_START} / {_END} markers are gone from create-worktree, so this suite "
        "is asserting nothing — fix the markers rather than deleting the test")
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "claim_the_branch" in block and "branch_issue" in block, (
        "the markers no longer bracket the claim stanza")
    return block


#: The colours and `die` the stanza uses, which live further up the real script.
PRELUDE = """
set -euo pipefail
RED=''; GREEN=''; YELLOW=''; CYAN=''; NC=''
die() { echo "Error: $1" >&2; exit 1; }
"""


def run_stanza(branch: str, *, claim="true", require="false", ttl="60",
              stub: str | None = None, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run `claim_the_branch` with a stub `qb-claim` of the caller's choosing.

    `stub=None` means no qb-claim on PATH at all, which is a real deployment
    state (a host with the board tooling absent) and one of the "cannot tell"
    cases the policy is about.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if stub is not None:
        fake = bindir / "qb-claim"
        fake.write_text("#!/usr/bin/env bash\n" + stub + "\n")
        fake.chmod(0o755)
    script = (PRELUDE
              + f'CLAIM={claim}\nREQUIRE_CLAIM={require}\nCLAIM_TTL={ttl}\n'
              + f'MAIN_REPO={tmp_path}\n'
              + claim_block()
              + f'\nclaim_the_branch {branch!r}\n')
    # PATH is the stub directory plus bash's OWN directory and nothing else. Not
    # the inherited PATH: the `stub=None` case has to mean "no qb-claim anywhere",
    # and on a machine where the harness is installed the real one is on PATH —
    # which would make that test assert the opposite of what it says.
    return subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True,
        env={"PATH": f"{bindir}:{os.path.dirname(BASH)}", "HOME": str(tmp_path)})


# ------------------------------------------------- the number comes off the branch

@pytest.mark.parametrize("branch,number", [
    ("feat/issue-172", "172"),
    ("fix/issue-114", "114"),
    ("refactor/issue-7", "7"),
    ("docs/issue-1", "1"),
    # The epic driver and the fix-issue skill both produce a trailing slug.
    ("feat/issue-135-qb-next", "135"),
    ("issue-42", "42"),
])
def test_the_issue_number_is_read_out_of_the_branch_name(branch, number, tmp_path):
    got = run_stanza(branch, tmp_path=tmp_path,
                     stub=f'echo "$@" >{tmp_path}/argv; echo id; exit 0')
    assert got.returncode == 0, got.stderr
    argv = (tmp_path / "argv").read_text().split()
    assert argv[:2] == ["issue", number], f"claimed the wrong thing: {argv}"


@pytest.mark.parametrize("branch", [
    "chore/review-policy-p1-p2",     # a real branch on this repo
    "feat/seats-dash-pane",
    "main",
    # NOT an issue: the digits have to be the whole tail of the `issue-` token, or
    # `issue-172x` and `myissue-9` would both claim numbers nobody meant.
    "feat/issue-172x",
    "feat/reissue-5",
])
def test_a_branch_that_names_no_issue_claims_nothing(branch, tmp_path):
    got = run_stanza(branch, tmp_path=tmp_path,
                     stub=f'echo called >{tmp_path}/argv; exit 0')
    assert got.returncode == 0, got.stderr
    assert not (tmp_path / "argv").exists(), "claimed something off a branch with no issue"
    assert "unclaimed" in got.stderr
    assert "qb-claim issue" in got.stderr, (
        "an unclaimed checkout has to say how to claim by hand, or the warning is "
        "just noise the reader learns to scroll past")


# ------------------------------------------------------- what each answer costs

def test_a_claim_held_by_somebody_else_refuses_the_checkout(tmp_path):
    """The one hard failure. Two agents on one issue is what this prevents, and a
    409 is the board saying so definitely."""
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub='echo "held: zeus/thorn-spruce has it" >&2; exit 1')
    assert got.returncode != 0
    assert "already claimed" in got.stderr
    assert "--no-claim" in got.stderr, (
        "a refusal has to name the escape hatch, or the next agent's remedy is to "
        "edit the script")


def test_an_unreachable_board_warns_and_proceeds(tmp_path):
    """A board outage must not stop every checkout on the fleet."""
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub='echo "unknown: board unreachable" >&2; exit 2')
    assert got.returncode == 0, got.stderr
    assert "unclaimed" in got.stderr
    assert "must not stop a checkout" in got.stderr


def test_require_claim_turns_every_uncertainty_into_a_refusal(tmp_path):
    """The strict reading of the issue's "claims atomically or refuses", for a
    caller that wants it — an epic driver, or a fleet that has finished enrolling."""
    for stub, why in [('exit 2', "board down"), (None, "qb-claim absent")]:
        got = run_stanza("feat/issue-9", require="true", tmp_path=tmp_path, stub=stub)
        assert got.returncode != 0, f"{why} was allowed through --require-claim"
        assert "--require-claim" in got.stderr
    # ...including a branch with nothing to claim, which is the case a strict
    # caller most wants to hear about: it means the branch was named by hand.
    got = run_stanza("main", require="true", tmp_path=tmp_path, stub='exit 0')
    assert got.returncode != 0 and "unclaimed checkout" in got.stderr


def test_a_missing_qb_claim_does_not_abort_the_run_under_set_e(tmp_path):
    """`create-worktree` runs under `set -euo pipefail`, where an absent sibling
    script aborts the run it was meant to inform — the failure mode `holder_note`
    already carries a comment about."""
    got = run_stanza("feat/issue-9", tmp_path=tmp_path, stub=None)
    assert got.returncode == 0, got.stderr
    assert "qb-claim is not on PATH" in got.stderr


def test_no_claim_skips_the_whole_thing(tmp_path):
    got = run_stanza("feat/issue-9", claim="false", tmp_path=tmp_path,
                     stub=f'echo called >{tmp_path}/argv; exit 0')
    assert got.returncode == 0, got.stderr
    assert not (tmp_path / "argv").exists()
    assert got.stderr == "", "--no-claim should be silent, not merely harmless"


def test_the_ttl_and_the_repo_are_passed_through(tmp_path):
    """A worktree outlives an hour, and a lapsed claim reads as free to the next
    agent — so the checkout claim is held for its own configured span, and it is
    keyed on the MAIN repo rather than on whatever directory the caller is in."""
    got = run_stanza("feat/issue-172", ttl="28800", tmp_path=tmp_path,
                     stub=f'echo "$@" >{tmp_path}/argv; exit 0')
    assert got.returncode == 0, got.stderr
    argv = (tmp_path / "argv").read_text()
    assert "--ttl 28800" in argv
    assert f"--repo-path {tmp_path}" in argv
    assert "--note" in argv, "a claim with no note is an obstruction, not coordination"


# ------------------------------------------------------------- the flags exist

@pytest.mark.parametrize("flag", ["--no-claim", "--require-claim", "--claim-ttl"])
def test_the_flags_are_documented_in_the_usage_header(flag):
    """The header is the only documentation `create-worktree --help` has (there is
    no --help), so a flag missing from it is a flag nobody finds."""
    src = SCRIPT.read_text()
    header = src.split("# ============================================", 1)[0]
    assert flag in header, f"{flag} is not in the usage header"
