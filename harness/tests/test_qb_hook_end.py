"""`qb-hook`'s ending: what the board is told when a session stops (#277).

The lifecycle hook is where "the session is over" is *observed*, and until now the
only thing it did about that was hand off a transcript. Two consequences, both
measured on real fleets rather than reasoned about:

* **The release rode on the upload.** `handoff` is what released the lease, and it
  ran only when a blob had been pushed — so a session with no readable
  transcript, or a 45s upload timing out on a huge JSONL, never released anything
  and sat on the board as a live agent until its TTL ran out.
* **`/clear` said nothing at all.** Claude Code fires `SessionEnd` with
  `reason: clear` and then `SessionStart` with a NEW session id, and the hook
  treated that exactly like a normal exit — so the claims the previous
  conversation took stayed live under a conversation with no memory of them
  (#263).

These drive the real script as a subprocess against a stub `qb-env` and a stub
`curl`, which is the only way to see what it would have SENT. The alternative —
asserting on the text of the file — is what `test_claude_wiring.py` does for the
dispatch switch, and it cannot tell a branch that is present from one that runs.

Run: pytest harness/tests
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
HOOK = BIN / "qb-hook"

# jq and curl are not incidental: the hook exits 0 without either, so a sandbox
# missing one would run every test below against a hook that no-op'd and report
# green. The flake's `worktree-tests` installs both for exactly this reason.
pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="qb-hook is bash and parses its payload with jq",
)


class Hooked:
    """A copy of `qb-hook` with a stub board beside it and a stub curl in front."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.bin = tmp_path / "hookbin"
        self.bin.mkdir()
        (self.bin / "qb-hook").write_bytes(HOOK.read_bytes())
        (self.bin / "qb-hook").chmod(0o755)
        # `qb-env` is found BESIDE the script — the hook walks the symlink chain
        # one link at a time looking for it — so a copy in a directory of our own
        # is the seam that decides which board it thinks it has.
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
        # One line per call, arguments NUL-free and whitespace-joined: every
        # assertion below is a substring test on a URL or a JSON body, and the
        # hook never puts a newline in either.
        (self.stub / "curl").write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {self.calls}\n'
            "exit 0\n"
        )
        (self.stub / "curl").chmod(0o755)

        # A cwd that is deliberately not a git checkout: `repo` and `branch` then
        # stay empty and the hook makes no git subprocesses, which is one less
        # thing between the payload and the call under test.
        self.cwd = tmp_path / "notarepo"
        self.cwd.mkdir()
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()

    def env(self, **over) -> dict:
        base = {k: v for k, v in os.environ.items()
                if k not in ("TMUX", "TMUX_PANE", "QUARTERBACK_INSTANCE")}
        return {**base,
                "PATH": f"{self.stub}:{os.environ['PATH']}",
                "XDG_RUNTIME_DIR": str(self.run_dir),
                "HOME": str(self.root / "home"),
                **over}

    def fire(self, event: str, env: dict | None = None, **payload):
        body = {"session_id": "sid-1", "cwd": str(self.cwd),
                "transcript_path": "", **payload}
        got = subprocess.run([str(self.bin / "qb-hook"), event],
                             input=json.dumps(body), capture_output=True, text=True,
                             env=env or self.env(), timeout=60)
        assert got.returncode == 0, got.stderr  # fail-open, by contract
        return got

    def sent(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def to(self, path: str) -> list[str]:
        return [c for c in self.sent() if f"http://board.test{path}" in c]

    def wait_for(self, predicate, seconds: float = 5.0) -> bool:
        """Backgrounded work — the hook fires and forgets some calls, and a test
        that read the log once would be racing it."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False


@pytest.fixture
def hook(tmp_path):
    return Hooked(tmp_path)


# ------------------------------------------------------------------ SessionEnd


def test_a_session_that_ends_is_ended_on_the_board(hook):
    hook.fire("SessionEnd", reason="other")
    ended = hook.to("/session/end")
    assert len(ended) == 1, hook.sent()
    assert '"session":"sid-1"' in ended[0]
    assert '"reason":"finished"' in ended[0]


def test_a_clear_is_reported_as_a_context_reset(hook):
    """#263. `/clear` gives the pane a fresh conversation with no memory of the
    previous one, and the previous one's claims used to stay live. The hook is
    the only thing that sees this happen — Claude Code names the reason, and the
    session id in the payload is still the OLD one, which is exactly the key the
    claims were taken under."""
    hook.fire("SessionEnd", reason="clear")
    ended = hook.to("/session/end")
    assert len(ended) == 1, hook.sent()
    assert '"reason":"context_reset"' in ended[0]
    assert '"session":"sid-1"' in ended[0]


def test_a_reason_the_board_does_not_know_is_not_forwarded(hook):
    """The board's vocabulary is closed, and this hook is fail-open — so a 422 it
    swallowed would leave the session unended for the sake of a label. Anything
    that is not `clear` is the session going away, which is `finished`."""
    hook.fire("SessionEnd", reason="some-future-reason")
    assert '"reason":"finished"' in hook.to("/session/end")[0]


def test_a_claude_code_that_sends_no_reason_still_ends_the_session(hook):
    hook.fire("SessionEnd")
    assert '"reason":"finished"' in hook.to("/session/end")[0]


def test_the_ending_does_not_ride_on_the_transcript_upload(hook):
    """The bug the swap from `handoff` to `snapshot` fixes. `transcript_path` is
    empty here — no blob, nothing to hand off — and that used to mean the lease
    was never released at all and the pane read as a live agent for its whole
    TTL. The two facts are independent and are now two calls."""
    hook.fire("SessionEnd", reason="other", transcript_path="")
    assert hook.to("/handoff") == []
    assert len(hook.to("/session/end")) == 1


def test_the_presence_post_names_an_unusual_ending_and_not_an_ordinary_one(hook):
    """A parenthesis on every session's last line would be the noisiest thing on
    the board; a `/clear` that reads like a finish is a wrong one."""
    hook.fire("SessionEnd", reason="clear")
    assert any("context_reset" in c for c in hook.to("/post"))

    second = hook.root / "second"
    second.mkdir()
    other = Hooked(second)
    other.fire("SessionEnd", reason="other")
    posts = other.to("/post")
    assert posts and not any("finished" in c for c in posts)


# ---------------------------------------------------------------- SessionStart


def test_a_new_conversation_in_the_same_pane_ends_the_one_before_it(hook):
    """The backstop for the endings nothing observed — a killed pane, a crashed
    CLI, a hook that could not reach the board at the time. Keyed on the INSTANCE
    because that is what a seat keeps across a restart: "the seat is the identity;
    the conversation is the worker"."""
    env = hook.env(QUARTERBACK_INSTANCE="seat-4")
    hook.fire("SessionStart", env=env, session_id="sid-old")
    assert hook.to("/session/end") == []  # nothing was there before it

    hook.fire("SessionStart", env=env, session_id="sid-new")
    ended = hook.to("/session/end")
    assert len(ended) == 1, hook.sent()
    assert '"session":"sid-old"' in ended[0]
    # `superseded`, not `context_reset`: all this knows is that a different
    # conversation is now in this pane. SessionEnd knows why, and fires first.
    assert '"reason":"superseded"' in ended[0]


def test_the_same_session_starting_again_ends_nothing(hook):
    """A resume, or a second SessionStart for one conversation. Ending the
    session that is starting would release the claims it is about to rely on."""
    env = hook.env(QUARTERBACK_INSTANCE="seat-5")
    hook.fire("SessionStart", env=env, session_id="sid-same")
    hook.fire("SessionStart", env=env, session_id="sid-same")
    assert hook.to("/session/end") == []


def test_a_session_with_no_instance_never_supersedes_anything(hook):
    """Without QUARTERBACK_INSTANCE the instance IS the session-id prefix, so the
    record is per-session and can never hold a different one. A plain `claude` in
    a window has no pane identity to inherit, and inventing one would let two
    unrelated sessions end each other."""
    hook.fire("SessionStart", session_id="aaaaaaaa-1")
    hook.fire("SessionStart", session_id="bbbbbbbb-2")
    assert hook.to("/session/end") == []


def test_the_pane_is_stamped_with_its_session_so_a_button_can_end_it(hook):
    """Nothing stamped a pane with its session id, which #266 names as the honest
    limit of what a fleet view can act on. `qb-seat-click`'s ✕ reads this stamp to
    end the agent before it kills the pane."""
    tmux_log = hook.root / "tmux.log"
    (hook.stub / "tmux").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {tmux_log}\n'
        "exit 0\n")
    (hook.stub / "tmux").chmod(0o755)

    hook.fire("SessionStart", env=hook.env(TMUX="/tmp/sock,1,0", TMUX_PANE="%7"),
              session_id="sid-pane")
    assert hook.wait_for(
        lambda: tmux_log.exists() and "@qb_session sid-pane" in tmux_log.read_text()
    ), tmux_log.read_text() if tmux_log.exists() else "tmux was never called"
