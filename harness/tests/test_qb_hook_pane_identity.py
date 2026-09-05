"""The pane, the conversation in it, and the seam between them (#146, #263).

Two facts used to hang off one string — the Claude Code session id — and they
want opposite things of it:

* **who this agent is** must be STABLE. `/clear` mints a NEW session id; the hook
  followed it within the second and `qb-mcp`, spawned once and never respawned,
  could not. One agent then had two names, two leases and two inboxes (#146).
* **what this conversation is holding** must NOT be stable. A conversation that
  remembers nothing of the one before it must not go on renewing its claims, and
  passive expiry can never reach those — nothing died (#263).

So the hook now derives both from the PANE: `QUARTERBACK_INSTANCE` when an
operator named it, otherwise the Claude Code CLI process that owns it, which
`CLAUDE_CODE_MESSAGING_SOCKET` names and a clear does not restart. The identity
follows the pane; the session stamp follows the conversation.

These drive the real script against the stub board `test_qb_hook_end.py` builds,
for the reason stated there: asserting on the text of the file cannot tell a
branch that is present from one that runs.

Run: pytest harness/tests
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_qb_hook_end import Hooked, pytestmark  # noqa: E402,F401

#: A pane that is a real Claude Code CLI process. The socket is named after that
#: process's pid, which is the property the whole join relies on: `/clear` gives
#: the pane a new conversation and does not restart the process.
SOCKET = "/run/user/1000/cc-socks/4242.sock"
PANE = "cli-4242"

#: Two conversations in one pane, as `/clear` leaves them. Eight characters of
#: prefix is what both halves key on, so the two must differ inside the first
#: eight or the test would pass on a bug.
FIRST = "aaaaaaaa-1111-4111-8111-111111111111"
SECOND = "bbbbbbbb-2222-4222-8222-222222222222"


@pytest.fixture
def hook(tmp_path):
    return Hooked(tmp_path)


def paned(hook, **over) -> dict:
    """The environment of a session running in a real Claude Code pane."""
    return hook.env(CLAUDE_CODE_MESSAGING_SOCKET=SOCKET, **over)


def instances(hook) -> list[str]:
    """The `X-Agent-Instance` values the hook has sent, in order, without repeats.

    One SessionStart makes several calls under one key — a lease, a presence
    post, an occupancy read — and this is about WHICH key, not how many times.
    """
    out: list[str] = []
    for call in hook.sent():
        _, _, rest = call.partition("X-Agent-Instance: ")
        key = rest.split(" ")[0] if rest else ""
        if key and (not out or out[-1] != key):
            out.append(key)
    return out


# --------------------------------------------------------- who am I (#146)


def test_a_clear_does_not_give_one_agent_two_identities(hook):
    """#146's whole subject. The MCP server's key is the prefix of the session id
    it was SPAWNED with, and no clear can move it; before this the hook took the
    prefix of whatever session id the payload carried, so from the first `/clear`
    the two halves of one agent addressed the board as two agents."""
    env = paned(hook)
    hook.fire("SessionStart", env=env, session_id=FIRST, source="startup")
    hook.fire("SessionStart", env=env, session_id=SECOND, source="clear")

    keys = set(instances(hook))
    assert keys == {"aaaaaaaa"}, hook.sent()


def test_a_seat_keeps_its_name_across_a_restart(hook):
    """The property that must SURVIVE this change. `qb-seats` sets
    QUARTERBACK_INSTANCE on every seat, which is why a seat never saw #146 — and
    it is still the first thing consulted, ahead of any pane derivation."""
    env = paned(hook, QUARTERBACK_INSTANCE="seat-quarterback-4")
    hook.fire("SessionStart", env=env, session_id=FIRST, source="startup")
    hook.fire("SessionStart", env=env, session_id=SECOND, source="startup")

    assert set(instances(hook)) == {"seat-quarterback-4"}, hook.sent()


def test_a_host_with_no_pane_signal_is_left_exactly_as_it_was(hook):
    """Nothing here may make a session worse off than it was. With neither an
    operator's label nor a CLI socket there is no pane to speak of, and the
    session id is all the identity there is — which is what shipped."""
    hook.fire("SessionStart", env=hook.env(), session_id=FIRST, source="startup")
    hook.fire("SessionStart", env=hook.env(), session_id=SECOND, source="clear")

    assert set(instances(hook)) == {"aaaaaaaa", "bbbbbbbb"}, hook.sent()
    # …and with no pane there is nothing that could have superseded anything:
    # two unrelated windows must never be able to end each other's sessions.
    assert hook.to("/session/end") == []


def test_a_child_session_does_not_speak_as_its_parent(hook):
    """A Task sub-agent is its own conversation and shares its parent's CLI
    process, so it would join the parent's pane and post under the parent's name.
    Worse, it would read the parent's session out of the supersede record and END
    IT — handing back claims the parent was still working. It takes no part."""
    parent = paned(hook)
    hook.fire("SessionStart", env=parent, session_id=FIRST, source="startup")
    child = paned(hook, CLAUDE_CODE_CHILD_SESSION="1")
    hook.fire("SessionStart", env=child, session_id=SECOND, source="startup")

    assert instances(hook) == ["aaaaaaaa", "bbbbbbbb"], hook.sent()
    assert hook.to("/session/end") == [], "a sub-agent ended its parent's session"
    assert (hook.run_dir / f"qb-conv-{PANE}").read_text() == FIRST


def test_the_pane_key_qb_mcp_wrote_is_the_one_adopted(hook):
    """qb-mcp is the authority on this value — the prefix it was spawned with IS
    its key — so when it has written one down the hook takes that, rather than
    guessing from a payload that may already have moved on. This is the upgrade
    case: a pane whose conversation was cleared before the hook learned to
    record one."""
    (hook.run_dir / f"qb-pane-{PANE}").write_text("cccccccc")
    hook.fire("SessionStart", env=paned(hook), session_id=SECOND, source="clear")

    assert instances(hook) == ["cccccccc"], hook.sent()


def test_a_recycled_pid_does_not_inherit_the_last_pane_that_had_it(hook):
    """The one hazard in deriving a pane from a pid. A pane that was killed rather
    than closed leaves its pointer behind for whoever gets its number next; a
    `startup` is by definition the pane's first conversation, so nothing older can
    be true and the record is rewritten rather than read."""
    (hook.run_dir / f"qb-pane-{PANE}").write_text("cccccccc")
    hook.fire("SessionStart", env=paned(hook), session_id=SECOND, source="startup")

    assert instances(hook) == ["bbbbbbbb"], hook.sent()
    assert (hook.run_dir / f"qb-pane-{PANE}").read_text() == "bbbbbbbb"


# ------------------------------------------- what am I holding (#146, #263)


def test_the_conversation_qb_mcp_should_stamp_is_written_down(hook):
    """The other half of the seam. qb-mcp's environment froze at spawn, so this
    file is the only way it can learn that the conversation it is serving is not
    the one it was started for — and every claim it takes is stamped with the
    answer."""
    env = paned(hook)
    hook.fire("SessionStart", env=env, session_id=FIRST, source="startup")
    assert (hook.run_dir / f"qb-conv-{PANE}").read_text() == FIRST

    hook.fire("SessionStart", env=env, session_id=SECOND, source="clear")
    assert (hook.run_dir / f"qb-conv-{PANE}").read_text() == SECOND


def test_a_reset_conversation_stops_holding_the_previous_ones_claims(hook):
    """#263. The claims were renewed indefinitely from a conversation that could
    not say what it was holding, and expiry could never reach them because
    nothing had died. The end names the PREVIOUS session, which is the key the
    claims were taken under, and says why."""
    env = paned(hook)
    hook.fire("SessionStart", env=env, session_id=FIRST, source="startup")
    assert hook.to("/session/end") == [], "nothing was there before it"

    hook.fire("SessionStart", env=env, session_id=SECOND, source="clear")
    ended = hook.to("/session/end")
    assert len(ended) == 1, hook.sent()
    assert f'"session":"{FIRST}"' in ended[0]
    assert '"reason":"context_reset"' in ended[0]


def test_a_resume_keeps_the_claims_it_still_owns(hook):
    """The cure that would be worse than the disease. `SessionStart` fires for
    `resume` as readily as for `clear`, and a resume is the SAME conversation
    continuing — releasing its claims would be #263 with the sign flipped. It is
    told apart two ways: the source says so, and a resume reuses the session id."""
    env = paned(hook)
    hook.fire("SessionStart", env=env, session_id=FIRST, source="startup")
    hook.fire("SessionStart", env=env, session_id=FIRST, source="resume")

    assert hook.to("/session/end") == [], hook.sent()
    assert (hook.run_dir / f"qb-conv-{PANE}").read_text() == FIRST


def test_a_pane_that_really_ends_leaves_no_pointers_behind(hook):
    """The pane key is derived from a CLI pid and the OS reuses those. A pane
    that is going must not leave a pointer for the next process to inherit — but
    a `clear` is the pane living ON, and clearing its pointers there would send
    qb-mcp back to its own frozen environment for exactly the window the reset
    happens in."""
    env = paned(hook)
    hook.fire("SessionStart", env=env, session_id=FIRST, source="startup")

    hook.fire("SessionEnd", env=env, session_id=FIRST, reason="clear")
    assert (hook.run_dir / f"qb-conv-{PANE}").exists()

    hook.fire("SessionEnd", env=env, session_id=FIRST, reason="other")
    assert not (hook.run_dir / f"qb-conv-{PANE}").exists()
    assert not (hook.run_dir / f"qb-pane-{PANE}").exists()
