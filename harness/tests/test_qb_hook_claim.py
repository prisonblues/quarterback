"""What `qb-hook` will publish as a claim, and what it will not (#157).

Every `UserPromptSubmit` used to publish the turn twice: as the lease's `recap`
and as a `working on:` status post. Both were gated on nothing but `.prompt`
being non-empty, and a prompt is not the same thing as a person declaring work.

A 240-minute sample of 34 auto-claims held 9 that were not work — peer messages,
task notifications, scripted sessions. The status post is the half you can see
and the smaller one. The lease write is the damage: it had no throttle at all, so
an agent messaged five times in ten minutes had the field `/overlap` ranks peers
on overwritten five times with the sender's socket-path wrapper. For that window
it was undiscoverable on what it was actually doing, which is the exact failure
the claim was written to prevent — and a peer who checks and is told the coast is
clear acts on the answer. A corrupted claim is worse than no claim.

So the two writes share one gate now. The asymmetry the tests below pin is
deliberate and runs the other way from what a spam filter would do: nothing here
tries to be sure a turn IS junk. A missing claim is the failure this whole
subsystem exists to prevent, so only a turn whose OPENING is a wrapper the client
itself wrote is refused, and a prompt that merely mentions one still claims.

`_lease` keeps firing on every prompt either way — it is the presence heartbeat
and the `working` state beacon, and presence is not a claim. What a refused turn
withholds is the `recap` field, which the board only overwrites when it is sent.

These drive the real script as a subprocess against a stub board, for the reason
`test_qb_hook_end.py` gives: asserting on the text of the file cannot tell a
branch that is present from one that runs.

Run: pytest harness/tests
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
HOOK = BIN / "qb-hook"
CLASSIFY = BIN / "qb-classify-command"

#: A peer's message, as Claude Code delivers it: one lead-in line the client
#: writes, then the wrapper. Post 3434 on the board is this text published under
#: the *recipient's* name as the recipient's declared work.
PEER_MESSAGE = (
    "Another Claude session sent a message:\n"
    '<cross-session-message from="uds:/tmp/cc-socks/3427180.sock" '
    'from-name="quarterback-2f" from-mode="bypass">\n'
    "zeus/marble-bronze. Nothing needed from you — just flagging that I am in the "
    "same file.\n"
    "</cross-session-message>"
)

#: A subagent finishing. The commonest of the three sources by a distance: 298 of
#: 1863 user turns in a two-week transcript sample opened with this wrapper.
TASK_NOTIFICATION = (
    "<task-notification>\n<task-id>a2ed7ee9c90a84f3f</task-id>\n"
    "<tool-use-id>toolu_014shgzwAUjNSNJNG6nKJLsp</tool-use-id>\n"
    "<output-file>/tmp/claude-1000/agent-out.txt</output-file>\n"
    "<status>completed</status>\n</task-notification>"
)

#: What a person taking something on actually looks like.
REAL_WORK = "Fix the flaky worktree test in harness/tests — it fails on hermes only."

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None
    or shutil.which("bash") is None
    or shutil.which("git") is None,
    reason="qb-hook is bash, parses its payload with jq, and this asks git",
)


class Claiming:
    """`qb-hook` over a real checkout, with a stub board recording what it sent."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.bin = tmp_path / "hookbin"
        self.bin.mkdir()
        for tool in (HOOK, CLASSIFY):
            (self.bin / tool.name).write_bytes(tool.read_bytes())
            (self.bin / tool.name).chmod(0o755)
        # `qb-env` is found BESIDE the script, so a copy in a directory of our own
        # is the seam that decides which board the hook thinks it has.
        (self.bin / "qb-env").write_text(
            "qb_load_config() {\n"
            "  QUARTERBACK_BASE_URL=http://board.test\n"
            "  QUARTERBACK_AGENT=testbox\n"
            "}\n"
            "qb_resolve_token() { QUARTERBACK_TOKEN=tok-test; return 0; }\n"
        )

        self.stub = tmp_path / "stub"
        self.stub.mkdir()
        self.calls = tmp_path / "curl.log"
        # An empty board: nobody else is live, nobody has asked us anything, and
        # nothing is behind. None of the per-turn notes compose, so the only calls
        # in the log are the ones under test.
        (self.stub / "curl").write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {self.calls}\n'
            'case "$*" in\n'
            '  *"/active"*) printf \'{"agents":[],"subagents":[]}\' ;;\n'
            '  *"/overlap"*) printf \'{"peers":[]}\' ;;\n'
            'esac\n'
            "exit 0\n"
        )
        (self.stub / "curl").chmod(0o755)
        # Silent, and stubbed here rather than by the tests that care: without it,
        # what the hook composes depends on whether the host running the suite has
        # `qb-mode` installed (#473).
        (self.stub / "qb-mode").write_text('#!/bin/sh\nprintf \'{"label":null}\'\n')
        (self.stub / "qb-mode").chmod(0o755)

        self.cwd = tmp_path / "checkout"
        self.cwd.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        self._git("remote", "add", "origin",
                  "git@github.com:prisonblues/quarterback.git")
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        (tmp_path / "home").mkdir()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.cwd), *args], check=True,
                       capture_output=True)

    def env(self) -> dict:
        base = {k: v for k, v in os.environ.items()
                if k not in ("TMUX", "TMUX_PANE", "QUARTERBACK_INSTANCE")}
        return {**base,
                "PATH": f"{self.stub}:{os.environ['PATH']}",
                "XDG_RUNTIME_DIR": str(self.run_dir),
                "HOME": str(self.root / "home")}

    def prompt(self, text: str) -> None:
        """One `UserPromptSubmit` carrying `text` as the turn."""
        body = {"session_id": "sid-157", "cwd": str(self.cwd),
                "transcript_path": "", "prompt": text}
        got = subprocess.run([str(self.bin / "qb-hook"), "UserPromptSubmit"],
                             input=json.dumps(body), capture_output=True, text=True,
                             env=self.env(), timeout=60)
        assert got.returncode == 0, got.stderr  # fail-open, by contract

    def sent(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def leases(self) -> list[str]:
        """Every `POST /lease` body, in order.

        Asserted non-empty rather than defaulted to `[]`: a rig that stopped
        leasing at all would make every "no recap was sent" test below pass by
        having nothing to disagree with.
        """
        found = [c for c in self.sent() if "http://board.test/lease" in c]
        assert found, f"the hook registered no lease at all: {self.sent()}"
        return found

    def recaps(self) -> list[str]:
        """The `recap` each lease carried, for the leases that carried one.

        An omitted field and an empty one are different answers: the board
        overwrites only what it is actually sent, so an absent `recap` leaves the
        last real subject standing while an empty one would erase it.
        """
        out = []
        for body in self.leases():
            found = re.search(r'"recap":\s*"([^"]*)"', body)
            if found:
                out.append(found.group(1))
        return out

    def claims(self) -> list[str]:
        """Every `working on:` status the hook posted to the board."""
        return [c for c in self.sent()
                if "http://board.test/post" in c and "working on:" in c]


@pytest.fixture
def hook(tmp_path):
    return Claiming(tmp_path)


# ----------------------------------------------------- a turn nobody typed


@pytest.mark.parametrize("turn, what", [
    (PEER_MESSAGE, "a peer's message"),
    (TASK_NOTIFICATION, "a subagent finishing"),
    ("<system-reminder>Keep the user's coding conventions in mind while you "
     "work on this.</system-reminder>", "an injected reminder"),
    ("<local-command-stdout>total 12\ndrwxr-xr-x 3 rich rich 4096 harness"
     "</local-command-stdout>", "a slash command's captured output"),
])
def test_an_injected_turn_writes_neither_a_claim_nor_a_subject(hook, turn, what):
    """Both writes, not just the visible one. The status post is what a board
    reader notices; the lease's `recap` is what `/overlap` ranks peers on, and
    patching only the post would leave the subject overwrite in place and make it
    harder to see rather than smaller."""
    hook.prompt(turn)
    assert hook.claims() == [], what
    assert hook.recaps() == [], f"{what} overwrote the peer-discovery subject"


def test_the_heartbeat_still_fires_on_a_turn_nobody_typed(hook):
    """Presence is not a claim, and refusing the claim must not cost the beacon.
    The lease is also the `working` state and the repo/branch registration every
    other surface reads, so the fix withholds one FIELD rather than the call."""
    hook.prompt(PEER_MESSAGE)
    assert hook.leases(), "the presence heartbeat went with the claim"


def test_a_prompt_that_merely_mentions_a_wrapper_still_claims(hook):
    """The gate is one-sided on purpose. It reads the OPENING of the turn, not the
    whole of it, because a missing claim is the failure this subsystem exists to
    prevent — and asking about this very bug is a prompt that quotes the wrapper."""
    hook.prompt("Look at how <task-notification> turns are handled in qb-hook "
                "and tell me whether the claim gate is right.")
    assert len(hook.claims()) == 1
    assert hook.recaps(), "a genuine prompt was refused for quoting a wrapper"


# ----------------------------------------------------- a person declaring work


def test_a_real_declaration_writes_both(hook):
    """The behaviour #157 must not break: one prompt, one subject on the lease and
    one status on the board."""
    hook.prompt(REAL_WORK)
    assert hook.recaps() == [REAL_WORK]
    assert len(hook.claims()) == 1
    assert "Fix the flaky worktree test" in hook.claims()[0]


def test_a_slash_command_is_a_person_declaring_work(hook):
    """`/fix-issue 157` arrives wrapped too, and the wrapper is how it was typed.
    Refusing every angle-bracketed turn would throw away the clearest declarations
    the fleet makes."""
    hook.prompt("<command-message>fix-issue is running…</command-message>"
                "<command-name>/fix-issue</command-name>"
                "<command-args>157</command-args>")
    assert len(hook.claims()) == 1
    assert hook.recaps()


def test_a_short_follow_up_claims_nothing_new(hook):
    """"yes" and "carry on" were already skipped for the post. They now skip the
    subject too — a two-word acknowledgement overwrites a good subject exactly as
    thoroughly as a socket path does."""
    hook.prompt("yes, carry on")
    assert hook.claims() == []
    assert hook.recaps() == []


# ----------------------------------------------------- the throttle


def test_a_second_declaration_refreshes_the_subject_but_does_not_post_again(hook):
    """The two writes are gated together and throttled apart, and that split is the
    point: the board sees one claim per ten minutes, while the lease keeps the
    subject current for the peer query that runs off it."""
    hook.prompt(REAL_WORK)
    hook.prompt("Now do the same for the hermes-only failure in test_qb_seats.")
    assert len(hook.claims()) == 1, "the 600s claim throttle stopped throttling"
    assert len(hook.recaps()) == 2, "the second declaration did not refresh the subject"


def test_an_injected_turn_does_not_spend_the_claim_slot(hook):
    """A regression the gate closes on its way past. The throttle used to be
    consumed by whichever turn reached it first, so a peer message arriving before
    a real prompt silenced that prompt's claim for ten minutes — the throttle
    working exactly as designed, against the only post it exists to protect."""
    hook.prompt(PEER_MESSAGE)
    hook.prompt(REAL_WORK)
    assert len(hook.claims()) == 1
    assert "Fix the flaky worktree test" in hook.claims()[0]


def test_same_problem_discovery_does_not_run_on_a_subject_nobody_declared(hook):
    """`/overlap` is asked to match live peers against the prompt. A socket-path
    wrapper can only produce a false "another agent is also on this", and asking
    spends the 10-minute discovery slot the next real prompt needs."""
    hook.prompt(TASK_NOTIFICATION)
    assert [c for c in hook.sent() if "/overlap" in c] == []
    hook.prompt(REAL_WORK)
    assert [c for c in hook.sent() if "/overlap" in c], (
        "discovery stopped running for genuine prompts too"
    )
