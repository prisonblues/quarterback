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

import json
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
              stub: str | None = None, py: str | None = None, after: str = "",
              tmp_path: Path) -> subprocess.CompletedProcess:
    """Run `claim_the_branch` with a stub `qb-claim` of the caller's choosing.

    `stub=None` means no qb-claim on PATH at all, which is a real deployment
    state (a host with the board tooling absent) and one of the "cannot tell"
    cases the policy is about.

    `py` stubs the `python3` the rollback releases THROUGH, and `after` is script
    run once the claim has been taken — which is where the rest of the checkout
    would be, and therefore where its failure has to be simulated. The EXIT trap
    fires either way, so a test that passes neither is asserting that nothing was
    released, which is also a thing worth asserting.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("qb-claim", stub), ("python3", py)):
        if body is None:
            continue
        fake = bindir / name
        fake.write_text("#!/usr/bin/env bash\n" + body + "\n")
        fake.chmod(0o755)
    script = (PRELUDE
              + f'CLAIM={claim}\nREQUIRE_CLAIM={require}\nCLAIM_TTL={ttl}\n'
              + f'MAIN_REPO={tmp_path}\n'
              + claim_block()
              + f'\nclaim_the_branch {branch!r}\n'
              + after + "\n")
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


# --------------------------------------- the session the claim is stamped with

def test_the_checkout_claim_records_NO_session(tmp_path):
    """Round 2's F04. `qb-claim` defaults `--session` to $CLAUDE_CODE_SESSION_ID,
    which here is the session of whoever RAN create-worktree — a parent agent's,
    or nothing at all from a human shell. The agent that will work in this tree
    has a different session and does not exist yet, so stamping the creating one
    mis-attributes the claim twice over: `/claim/held` narrows on the session, so
    the pickup gate this exists to feed read the claim as somebody else's and
    reported the new worktree free; and `may_mutate` requires a recorded session
    to match, so the worktree's own agent got a 403 renewing or releasing its own
    checkout claim.

    A claim that names no session falls back to the machine, which is what a
    checkout claim is: it belongs to the tree until somebody picks it up.
    """
    argv = tmp_path / "argv"
    got = run_stanza("feat/issue-172", tmp_path=tmp_path,
                     stub=f'printf "%s\\n" "$@" >{argv}; echo cid; exit 0',
                     after="CLAIM_KEPT=true")
    assert got.returncode == 0, got.stderr
    lines = argv.read_text().split("\n")
    assert "--session" in lines, f"the session was left to the environment: {lines}"
    assert lines[lines.index("--session") + 1] == "", (
        "the checkout stamped a session on a claim it does not own")
    assert "--json" in lines, (
        "the rollback reads `renewed` off the board's answer, so the claim has to be "
        "taken with --json — otherwise stdout is a bare id and a renew looks like a take")


def test_the_environments_session_is_not_inherited(tmp_path):
    """The same property from the other side: even with a session id in the
    environment — which is the normal state of every agent shell — the claim is
    taken without one."""
    argv = tmp_path / "argv"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "qb-claim"
    # A stub that behaves like the real default: fall back to the environment.
    # argparse's own semantics: the environment supplies the DEFAULT, and any
    # `--session` on the command line replaces it — including an empty one. A stub
    # that fell back on empty would be testing itself rather than the stanza.
    fake.write_text(
        '#!/usr/bin/env bash\n'
        'sess="${CLAUDE_CODE_SESSION_ID:-}"\n'
        'while [[ $# -gt 0 ]]; do\n'
        '  if [[ "$1" == "--session" ]]; then sess="$2"; shift 2; else shift; fi\n'
        'done\n'
        f'printf "%s" "$sess" >{argv}\n'
        'echo \'{"claim_id":"cid"}\'\n')
    fake.chmod(0o755)
    script = (PRELUDE
              + 'CLAIM=true\nREQUIRE_CLAIM=false\nCLAIM_TTL=60\n'
              + f'MAIN_REPO={tmp_path}\n'
              + claim_block()
              + "\nclaim_the_branch 'feat/issue-172'\nCLAIM_KEPT=true\n")
    got = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True,
        env={"PATH": f"{bindir}:{os.path.dirname(BASH)}", "HOME": str(tmp_path),
             "CLAUDE_CODE_SESSION_ID": "the-parents-session"})
    assert got.returncode == 0, got.stderr
    assert argv.read_text() == "", (
        "the parent's session id reached the claim, which is the F04 defect")


# ------------------------------- and it is handed back if the tree never exists

def test_a_failed_checkout_hands_the_claim_back(tmp_path):
    """Round 2's F17. The claim is taken before `git worktree add` on purpose — a
    refusal has then cost nothing. The inverse was not handled: the claim
    succeeds, the tree does not (branch checked out elsewhere, disk full, a bad
    base ref, a failing .env step), the script dies under `set -euo pipefail`, and
    the issue stays held for CLAIM_TTL — 8h by default — by an agent that does not
    exist. Worse than not claiming: the record reads as authoritative and there is
    nobody to talk to."""
    released = tmp_path / "released"
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub=f'echo \'{{"claim_id":"claim-abc"}}\'; exit 0',
                     py=f'printf "%s" "$QB_CLAIM_ANSWER" >{released}; echo released; exit 0',
                     after="false")
    assert got.returncode != 0
    assert released.exists(), "the claim was left held by a checkout that never happened"
    assert "claim-abc" in released.read_text(), (
        "the board's own answer is what the rollback acts on — capturing stdout is "
        "the whole reason it is captured")
    assert "claim released" in got.stderr


def test_the_rollback_does_not_change_the_exit_status(tmp_path):
    """A cleanup that rewrote the exit code would hide the real failure behind its
    own success."""
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub=f'echo \'{{"claim_id":"claim-abc"}}\'; exit 0',
                     py='echo released; exit 0', after="exit 7")
    assert got.returncode == 7


def test_a_completed_checkout_KEEPS_the_claim(tmp_path):
    """Past the point the tree exists the claim belongs to something somebody can
    work in or remove, and releasing it would be the opposite error."""
    released = tmp_path / "released"
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub=f'echo \'{{"claim_id":"claim-abc"}}\'; exit 0',
                     py=f'touch {released}; echo released; exit 0', after="CLAIM_KEPT=true")
    assert got.returncode == 0, got.stderr
    assert not released.exists(), "a successful checkout released its own claim"


def test_a_refusal_releases_nothing(tmp_path):
    """There is nothing to hand back: the 409 means somebody else holds it, and a
    release attempt would be an attempt on their claim."""
    released = tmp_path / "released"
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub='echo "held: zeus/thorn-spruce has it" >&2; exit 1',
                     py=f'touch {released}; echo released; exit 0')
    assert got.returncode != 0 and "already claimed" in got.stderr
    assert not released.exists()


def test_a_release_that_fails_says_what_to_release_by_hand(tmp_path):
    """An unreachable board is the likeliest reason the checkout failed at all, so
    the rollback is best-effort — and when it cannot run, the id it could not use
    is the one thing the operator needs."""
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub=f'echo \'{{"claim_id":"claim-abc"}}\'; exit 0',
                     py='exit 1', after="false")
    assert got.returncode != 0
    assert "claim NOT released" in got.stderr
    assert "claim-abc" in got.stderr, (
        "the board's answer was not printed, so nobody can act on it")


def test_no_python3_is_not_a_crash(tmp_path):
    """The release goes through qbdata's own client, so it needs an interpreter.
    A host without one must still get its real error, not a second one from the
    cleanup."""
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub=f'echo \'{{"claim_id":"claim-abc"}}\'; exit 0', after="false")
    assert got.returncode != 0
    assert "claim NOT released" in got.stderr


def test_a_RENEWED_claim_is_not_handed_back(tmp_path):
    """`qb-claim` exits 0 both for a claim it took and for one this machine already
    held. Releasing the second would destroy a claim that existed before the run —
    an agent that had #9 by hand, ran a checkout for it, and lost its claim to the
    checkout's failure. The board's own `renewed` flag decides, which is why the
    claim is taken with `--json`: grepping qb-claim's prose is the thing its
    docstring says a caller should not have to do."""
    released = tmp_path / "released"
    got = run_stanza("feat/issue-9", tmp_path=tmp_path,
                     stub='echo \'{"claim_id":"claim-abc","renewed":true}\'; exit 0',
                     py=(f'if [[ "$QB_CLAIM_ANSWER" == *renewed* ]]; then '
                         f'echo "left alone: already ours"; exit 0; fi; '
                         f'touch {released}; echo released; exit 0'),
                     after="false")
    assert got.returncode != 0
    assert not released.exists(), (
        "a claim this machine already held was released by a failed checkout")
    assert "left alone" in got.stderr


def test_the_real_rollback_leaves_a_renewed_claim_alone(tmp_path):
    """The same property through the REAL python the trap runs, not a stub of it:
    a `renewed: true` answer must not reach `POST /claim/release` at all. The stub
    here is a fake `qbdata` module, so importing it at all is the failure."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "qbdata.py").write_text(
        "def board_client():\n"
        f"    open({str(tmp_path / 'touched')!r}, 'w').close()\n"
        "    raise AssertionError('the rollback tried to release a renewed claim')\n")
    stub = bindir / "qb-claim"
    stub.write_text('#!/usr/bin/env bash\necho \'{"claim_id":"c1","renewed":true}\'\n')
    stub.chmod(0o755)
    script = (PRELUDE
              + 'CLAIM=true\nREQUIRE_CLAIM=false\nCLAIM_TTL=60\n'
              + f'MAIN_REPO={tmp_path}\n'
              + claim_block()
              + "\nclaim_the_branch 'feat/issue-9'\nfalse\n")
    got = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                         env={"PATH": f"{bindir}:{os.path.dirname(BASH)}:/usr/bin:/bin",
                              "HOME": str(tmp_path)})
    assert got.returncode != 0
    assert not (tmp_path / "touched").exists(), got.stderr
    assert "left alone" in got.stderr, got.stderr


def test_the_real_rollback_posts_the_release_through_qbdata(tmp_path):
    """The release path of the same trap, through the real python: the claim_id off
    the board's answer, posted to `/claim/release` via **qbdata's own client**. Not
    a curl — the base URL, the token and its config precedence live in that module,
    and a second implementation of "where is the board" is the class of defect #172
    is about."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "qbdata.py").write_text(
        "import json\n"
        "class _C:\n"
        "    def post(self, path, body):\n"
        f"        open({str(tmp_path / 'posted')!r}, 'w').write(json.dumps([path, body]))\n"
        "        return {}\n"
        "def board_client():\n"
        "    return _C(), None\n")
    stub = bindir / "qb-claim"
    stub.write_text('#!/usr/bin/env bash\necho \'{"claim_id":"c9"}\'\n')
    stub.chmod(0o755)
    script = (PRELUDE
              + 'CLAIM=true\nREQUIRE_CLAIM=false\nCLAIM_TTL=60\n'
              + f'MAIN_REPO={tmp_path}\n'
              + claim_block()
              + "\nclaim_the_branch 'feat/issue-9'\nfalse\n")
    got = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                         env={"PATH": f"{bindir}:{os.path.dirname(BASH)}:/usr/bin:/bin",
                              "HOME": str(tmp_path)})
    assert got.returncode != 0
    assert (tmp_path / "posted").exists(), got.stderr
    path, body = json.loads((tmp_path / "posted").read_text())
    assert path == "/claim/release"
    assert body == {"claim_id": "c9"}
    assert "released" in got.stderr
