"""The line that gives a checkout's claim an owner — #681's half of the pickup gate.

`create-worktree` claims the issue its branch names, and takes it for the MACHINE
with `session: null`. That is right, and `test_create_worktree_claim.py` pins it:
the agent that will work in the tree does not exist yet, so stamping the creating
session mis-attributes the claim and locks the eventual worktree agent out of its
own row.

What was never built is the other end. A machine claim is outside every automatic
release the fleet has — `POST /session/end` frees the claims a SESSION took, and
`qb-reconcile` looks a holder up in `/active`, which lists leases, so a bare
machine name can never appear there. Nine such claims stood on zeus the day #681
was written and three named issues that had closed seven hours earlier.

So the claim is handed over rather than re-attributed, and the handover is one
`qb-claim` from inside the working session: the board reads that as a renew by the
same machine and stamps the session onto the row that is already there. The
instruction to run it is written into the worktree's own `CLAUDE.local.md`, which
is what an agent opening the tree actually reads — whichever route brought it
there, including the ones no command brief covers (a human's `create-worktree`,
the epic driver, `qb-start`).

Three properties:

* it is written when a claim is actually standing, and names that issue;
* it is written when NOTHING was claimed — it is not. A note telling an agent to
  adopt a claim that was never taken reads as a record that exists, which is worse
  than the silence it replaces;
* the command it prints survives a paste, because a worktree path may contain a
  space and this note outlives the run that wrote it.

The block is extracted from the real script rather than copied, so a refactor that
moves or renames it fails here instead of leaving this suite green about code
nobody runs — the rule `test_create_worktree_claim.py` and
`test_create_worktree_db_name.py` both follow.

Run: pytest harness/tests
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "create-worktree"

BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not on PATH")

_START = "# >>> adopt"
_END = "# <<< adopt"


def adopt_block() -> str:
    """The context-note stanza, lifted out of create-worktree as it ships."""
    src = SCRIPT.read_text()
    assert src.count(_START) == 1 and src.count(_END) == 1, (
        f"the {_START} / {_END} markers are gone from create-worktree, or are no "
        "longer unique in it, so this suite is asserting nothing — fix the markers "
        "rather than deleting the test")
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "qb-claim issue" in block, "the markers no longer bracket the adoption note"
    return block


def sh_quote() -> str:
    """The real `sh_quote`, not a stand-in.

    The paste-safety property below is entirely about what that function does, so a
    stub would be the test asserting against itself. One line, lifted by name.
    """
    for line in SCRIPT.read_text().splitlines():
        if line.startswith("sh_quote()"):
            return line
    raise AssertionError("create-worktree no longer defines sh_quote")


def run_block(*, answer: str = '{"claim_id": "c1"}', issue: str = "713",
              ttl: str = "28800", worktree: str = "/home/rich/source/wt",
              branch: str = "fix/issue-713") -> subprocess.CompletedProcess:
    """Run the stanza with the variables it reads, under the script's own options.

    `set -euo pipefail` is on exactly as in `create-worktree`: an unset variable
    inside the note would be a crash at step 7 of 10, on a tree that already
    exists, which is a worse failure than the missing note.
    """
    program = "\n".join([
        "set -euo pipefail",
        sh_quote(),
        f"CLAIM_ANSWER={_q(answer)}",
        f"CLAIM_ISSUE={_q(issue)}",
        f"CLAIM_TTL={_q(ttl)}",
        f"WORKTREE_DIR={_q(worktree)}",
        f"BRANCH_NAME={_q(branch)}",
        adopt_block(),
    ])
    return subprocess.run([BASH, "-c", program], capture_output=True, text=True)


def _q(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def test_a_standing_claim_gets_a_note_naming_its_issue():
    got = run_block(issue="663")
    assert got.returncode == 0, got.stderr
    assert "#663" in got.stdout, (
        "the context note does not name the issue the checkout claimed, so an agent "
        "reading it cannot tell what to adopt")


def test_the_note_carries_the_command_that_adopts_it():
    got = run_block(issue="663", worktree="/home/rich/source/qb-663")
    assert "qb-claim issue 663" in got.stdout, (
        "the note describes the adoption without giving the command, which is the "
        "half an agent can act on")
    assert "--repo-path /home/rich/source/qb-663" in got.stdout, (
        "the adoption command does not name the worktree, so it would key on "
        "whatever repo the caller happened to be standing in")


def test_the_ttl_in_the_command_is_the_one_the_claim_was_taken_with():
    """A renew rewrites `expires_at` from the TTL it is sent, so a command carrying
    a different number would silently shorten or extend the claim it adopts —
    `qb-claim`'s own default is the board's hour, which is shorter than every
    checkout claim ever taken."""
    got = run_block(ttl="28800")
    assert "--ttl 28800" in got.stdout, (
        "the adoption command does not pass the checkout's own TTL, so adopting the "
        "claim would change how long it holds")
    assert "8h" in got.stdout, "the prose does not say how long the claim stands unadopted"


def test_the_stated_duration_is_derived_from_the_ttl_and_not_written_twice():
    """The hours in the sentence and the seconds in the command are one number, so a
    changed `CLAIM_TTL` cannot leave the note saying the old one."""
    got = run_block(ttl="10800")
    assert "--ttl 10800" in got.stdout
    assert "3h" in got.stdout, (
        "the note still says a duration the TTL does not support, so the two halves "
        "are separate literals and one of them is now wrong")


def test_nothing_is_written_when_nothing_was_claimed():
    """`CLAIM_ANSWER` is emptied on every path that took no claim — a branch naming
    no issue, no `qb-claim` on the host, a board that could not be reached. Telling
    an agent to adopt a claim that does not exist is worse than saying nothing: it
    reads as a record that exists, and the adoption would then take a fresh claim
    the checkout deliberately did not take."""
    got = run_block(answer="")
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "", (
        "the worktree is told to adopt a claim that was never taken:\n" + got.stdout)


def test_nothing_is_written_when_the_branch_named_no_issue():
    got = run_block(answer="", issue="")
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == ""


def test_the_command_survives_a_paste_from_a_worktree_path_with_a_space():
    """This note outlives the run that wrote it, and it is read by an agent that
    will paste the line rather than reason about it. `create-worktree`'s own
    `sh_quote` comment makes the same argument about every hint it prints."""
    got = run_block(worktree="/home/rich/my worktrees/wt-663")
    assert got.returncode == 0, got.stderr
    assert "--repo-path /home/rich/my worktrees/wt-663" not in got.stdout, (
        "the worktree path is pasted raw, so the adoption command splits into two "
        "arguments and claims against the wrong repo")
    assert "worktrees" in got.stdout


def test_the_note_says_what_each_exit_code_means():
    """`qb-claim`'s three codes are three different actions, and the difference is
    the whole of its own docstring: 1 names a peer to go and talk to, 2 is a board
    that could not be reached and stops nothing. A note that said only "run this"
    would leave an agent to guess which of the two it hit."""
    got = run_block()
    for code in ("Exit 0", "Exit 1", "Exit 2"):
        assert code in got.stdout, f"the note does not say what {code} means"


def test_the_claim_block_still_records_no_session():
    """The adoption exists BECAUSE the checkout claim names no session, so a change
    that stamped one here would leave two mechanisms doing the same job and the
    argument in `claim_the_branch`'s comment silently reversed.
    `test_create_worktree_claim.py` owns that property; this asserts the pointer
    between the two halves resolves, so neither can be moved without the other."""
    src = SCRIPT.read_text()
    claim = src.split("# >>> claim", 1)[1].split("# <<< claim", 1)[0]
    assert '--session ""' in claim, (
        "the checkout claim now records a session, which makes this whole suite's "
        "subject obsolete — delete it deliberately rather than leaving it green")
    assert "#681" in claim, (
        "the claim block no longer explains why a machine-held claim needs adopting, "
        "so the next reader meets `--session \"\"` with only the argument for it")
