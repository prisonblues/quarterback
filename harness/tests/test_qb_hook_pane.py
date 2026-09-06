"""The pane: what `qb-hook` records, and what it hands back on a reset (#146, #263).

`/clear` does not restart the Claude Code CLI. It mints a new conversation inside
the same process, so the two halves of one agent come apart: this hook is a fresh
process per event and moves to the new id immediately, while `qb-mcp` is spawned
once and never again and cannot. One agent, two board identities — and the claims
the previous conversation took stay live under a conversation with no memory of
taking them.

The hook already had a backstop for the second half, and until now it could never
fire. `_supersede_previous` looked the previous conversation up in a file keyed on
the INSTANCE, and an instance is a conversation unless somebody pinned one by hand
— `qb-seats` unsets `QUARTERBACK_INSTANCE` and puts nothing in its place (#540) —
so on this fleet the file was per-conversation, could never hold a different one,
and the whole path no-op'd. Every test that covered it therefore had to pin an
instance, which is a state no session on this fleet is in.

The record is now keyed on the PANE — the CLI process, named through
`CLAUDE_CODE_MESSAGING_SOCKET`, which a clear does not restart. So the backstop
fires for the sessions that actually exist, and the same file is what carries the
current conversation to the half of the agent that cannot see it.

**The one thing this must never do is take work off a live agent.** A resume is
the same conversation continuing and a sub-agent shares its parent's pane exactly;
both have their own test below, and both are refusals rather than arithmetic.

Run: pytest harness/tests
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_qb_hook_end import Hooked, pytestmark  # noqa: F401  (the jq/bash skip)

QB_ENV = Path(__file__).resolve().parents[1] / "bin" / "qb-env"

#: THE ENVIRONMENT CLAUDE CODE REALLY HANDS A HOOK, captured 2026-09-06 on
#: 2.1.258 and applied to every case below. It is here because leaving it out is
#: how the first cut of this file shipped a hook that refused every event in
#: production while eleven tests passed: `_path_sandbox.sandbox_env` builds an
#: environment from scratch and drops everything matching `CLAUDE_`, so every
#: test ran in a state production is never in, and a guard on
#: `CLAUDE_CODE_CHILD_SESSION` looked sound because the variable was absent.
#:
#: It is not absent. It is set on EVERY hook Claude Code runs. Measured by
#: starting a CLI with the whole parent environment stripped —
#: `env -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ID
#: -u CLAUDE_CODE_MESSAGING_SOCKET … claude -p` — and reading what its
#: SessionStart hook was handed. A session with no parent at all, and the
#: variable is `1`. It means "spawned by Claude Code", not "is a sub-agent".
#:
#: `CLAUDE_CODE_MESSAGING_SOCKET` is deliberately NOT here: it is the subject of
#: most of these tests and each supplies its own. Everything else is verbatim,
#: with the values that identify a particular run replaced.
HOOK_ENV = {
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_MESSAGING_TOKEN": "tok-messaging",
    "CLAUDE_PID": "1607423",
    "CLAUDE_PROJECT_DIR": "/home/rich/source/quarterback",
}


@pytest.fixture
def hook(tmp_path):
    """`qb-hook` over the REAL `qb-env`, with only the site config stubbed.

    Same shape as `test_requested_name`'s fixture and for the same reason:
    `test_qb_hook_end` stubs the library wholesale, which is right for the events
    it drives and wrong here, because the pane derivation IS that library.
    """
    h = Hooked(tmp_path)
    (h.bin / "qb-env").write_text(
        f'. "{QB_ENV}"\n'
        "qb_load_config() {\n"
        "  QUARTERBACK_BASE_URL=http://board.test\n"
        "  QUARTERBACK_AGENT=testbox\n"
        "}\n"
        "qb_resolve_token() { QUARTERBACK_TOKEN=tok-test; return 0; }\n"
    )
    return h


class Pane:
    """A socket file naming a live ancestor of whatever the test starts.

    `os.getpid()` is pytest, and pytest is the parent of every `qb-hook` this
    suite runs — which is the real relationship a Claude Code CLI has to the hook
    it spawns. Arranged with the actual process tree rather than a fake `/proc`,
    so what passes here is what passes on a fleet.
    """

    def __init__(self, root: Path, name: str = "cc-socks", mtime: int = 1_700_000_000):
        socks = root / name
        socks.mkdir(exist_ok=True)
        self.path = socks / f"{os.getpid()}.sock"
        self.path.write_bytes(b"")
        os.utime(self.path, (mtime, mtime))
        self.key = f"{os.getpid()}-{mtime}"

    def file_in(self, run_dir: Path) -> Path:
        return run_dir / f"qb-pane-{self.key}"


def started(hook, sid: str, source: str = "startup", **over):
    """A SessionStart, in the environment Claude Code actually supplies.

    `HOOK_ENV` is merged for EVERY case rather than for the one test that thinks
    it needs it. A hook that behaves differently under the real environment is
    exactly the bug this file was written to catch, and it cannot be caught by a
    suite whose default is an environment that never occurs.
    """
    env = hook.env(**{**HOOK_ENV, "CLAUDE_CODE_SESSION_ID": sid, **over})
    hook.fire("SessionStart", env=env, session_id=sid, source=source)


# ------------------------------------------------------- the reset that fires


def test_a_clear_ends_the_previous_conversation_with_no_instance_pinned(hook):
    """#263, for the sessions this fleet actually runs.

    No `QUARTERBACK_INSTANCE` — which is every session since #540 — so before the
    pane this path could not fire at all: the record was keyed on the conversation
    it was trying to notice had changed. The CLI process is the thing that did not
    change, and now it is what the record is named after.
    """
    pane = Pane(hook.root)
    env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path)}
    started(hook, "sid-old", **env)
    started(hook, "sid-new", source="clear", **env)

    ended = hook.to("/session/end")
    assert len(ended) == 1, hook.sent()
    assert '"session":"sid-old"' in ended[0]
    assert '"reason":"context_reset"' in ended[0]


def test_the_pane_carries_the_current_conversation_to_the_other_half(hook):
    """#146. `qb-mcp` cannot see this payload and is never respawned, so this file
    is the only way it learns that the conversation moved. The name is the CLI
    process, which is what makes it survive the thing that moved."""
    pane = Pane(hook.root)
    env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path)}
    marker = pane.file_in(hook.run_dir)

    started(hook, "sid-old", **env)
    assert marker.read_text() == "sid-old"
    started(hook, "sid-new", source="clear", **env)
    assert marker.read_text() == "sid-new"


def test_a_fork_is_not_a_reset_but_is_still_a_supersession(hook):
    """A fork carries memory forward, so it is not a context reset — and it does
    mint a new conversation, so a different one IS in this pane, which is what
    `superseded` claims and all it claims. An older Claude Code that sends no
    `source` at all lands in the same arm for the same reason."""
    for source in ("fork", None):
        root = hook.root / f"case-{source}"
        root.mkdir()
        other = Hooked(root)
        (other.bin / "qb-env").write_text((hook.bin / "qb-env").read_text())
        pane = Pane(root)
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path)}
        started(other, "sid-old", **env)
        if source is None:
            other.fire("SessionStart",
                       env=other.env(**{**HOOK_ENV, "CLAUDE_CODE_SESSION_ID": "sid-new",
                                        **env}),
                       session_id="sid-new")
        else:
            started(other, "sid-new", source=source, **env)
        ended = other.to("/session/end")
        assert len(ended) == 1, (source, other.sent())
        assert '"reason":"superseded"' in ended[0], source


def test_a_compact_hands_nothing_back_because_it_is_the_same_conversation(hook):
    """`/compact` does NOT mint a new conversation — verified in three
    transcripts, where the post-compaction entries keep the same `sessionId` and
    point back into the pre-compaction thread. So it reaches this path with the
    pane already holding its own id and no arm of the `case` runs. Written as
    the state that actually occurs rather than as the one the source name
    suggests: a compact with a DIFFERENT session id is not a thing to assert on."""
    pane = Pane(hook.root)
    env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path)}
    started(hook, "sid-same", **env)
    started(hook, "sid-same", source="compact", **env)
    assert hook.to("/session/end") == [], hook.sent()
    assert pane.file_in(hook.run_dir).read_text() == "sid-same"


# --------------------------------------------- the two that must never fire


def test_a_resume_hands_nothing_back(hook):
    """The cure that would be worse than the disease. `SessionStart` fires for
    resume as well as for clear, and a resume is the SAME work continuing —
    dropping its claims would be this issue with the sign flipped."""
    pane = Pane(hook.root)
    env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path)}
    started(hook, "sid-same", **env)
    started(hook, "sid-same", source="resume", **env)
    assert hook.to("/session/end") == [], hook.sent()


def test_a_resume_to_a_DIFFERENT_conversation_in_the_pane_hands_nothing_back(hook):
    """`--resume` reuses the session id, so the comparison above already excludes
    it. `/resume` INSIDE a pane picks a different conversation and reaches the
    release, so the refusal is by NAME rather than by arithmetic: the one thing
    this path must never do is hand back work somebody is still doing, and a
    cheap explicit refusal beats an implicit one for that."""
    pane = Pane(hook.root)
    env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path)}
    started(hook, "sid-old", **env)
    started(hook, "sid-other", source="resume", **env)
    assert hook.to("/session/end") == [], hook.sent()
    # …and the pane still moves, because the conversation did.
    assert pane.file_in(hook.run_dir).read_text() == "sid-other"


def test_a_start_carrying_the_panes_own_conversation_releases_nothing(hook):
    """WHAT ACTUALLY PROTECTS A TASK SUB-AGENT'S PARENT.

    A sub-agent shares its parent's messaging socket, so it computes the parent's
    pane exactly — and it shares the parent's `session_id` too. So if one ever
    reached this path it would find the pane already holding its own
    conversation, and hand nothing back. That is the arithmetic rather than a
    special case, and it is the property to pin, because the marker that looked
    like a discriminator is not one (see `HOOK_ENV`).

    Measured 2026-09-06 on 2.1.258: a run that launched a Task sub-agent produced
    exactly ONE `SessionStart` — the main session's — and one `SubagentStop`
    carrying that same session id. A sub-agent does not fire the event this path
    hangs off at all, so the arithmetic below is the second of two reasons rather
    than the only one.
    """
    pane = Pane(hook.root)
    env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path)}
    started(hook, "sid-parent", **env)
    started(hook, "sid-parent", **env)          # the sub-agent's id IS this one

    assert hook.to("/session/end") == [], hook.sent()
    assert pane.file_in(hook.run_dir).read_text() == "sid-parent"


def test_the_child_session_marker_decides_nothing(hook):
    """The regression guard for the bug this file shipped and had to take back.

    `CLAUDE_CODE_CHILD_SESSION=1` is in the environment of EVERY hook — a
    top-level session started with the whole parent environment stripped still
    gets it. A guard on it refuses everything, leaves the pane file unwritten and
    makes #146 and #263 inert while the suite reports green. So the outcome is
    required to be the SAME with it and without it: if a future change keys on it
    again, this fails rather than the fleet does.
    """
    outcomes = []
    for present in (True, False):
        root = hook.root / f"marker-{present}"
        root.mkdir()
        other = Hooked(root)
        (other.bin / "qb-env").write_text((hook.bin / "qb-env").read_text())
        pane = Pane(root)
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path)}
        if not present:
            env["CLAUDE_CODE_CHILD_SESSION"] = ""
        started(other, "sid-old", **env)
        started(other, "sid-new", source="clear", **env)
        outcomes.append((other.to("/session/end"),
                         pane.file_in(other.run_dir).read_text()))

    with_marker, without_marker = outcomes
    assert with_marker == without_marker, (
        "the hook behaves differently under the environment Claude Code really "
        "supplies than under the one this suite invents — which is how a guard "
        "that refused every event in production passed eleven tests")
    assert len(with_marker[0]) == 1 and '"reason":"context_reset"' in with_marker[0][0]
    assert with_marker[1] == "sid-new"


# ------------------------------------------------------- when there is no pane


def test_with_no_socket_at_all_nothing_is_superseded(hook):
    """A plain `claude` with no messaging socket, or a runtime that is not Claude
    Code. There is no pane to inherit, and inventing one would let two unrelated
    sessions end each other — which is worse than the bug."""
    started(hook, "sid-old")
    started(hook, "sid-new", source="clear")
    assert hook.to("/session/end") == [], hook.sent()
    assert not list(hook.run_dir.glob("qb-pane-*"))


def test_a_socket_whose_owner_is_alive_but_unrelated_owns_no_pane(hook):
    """The guard, exercised through the hook rather than through the derivation.
    A process can hold a socket variable it did not earn: measured on this fleet,
    a shell a dead CLI left behind still carries `3524155.sock`. Adopting that
    pane would mean speaking — and ending sessions — for somebody else's."""
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        socks = hook.root / "cc-socks"
        socks.mkdir()
        sock = socks / f"{stranger.pid}.sock"
        sock.write_bytes(b"")
        env = {"CLAUDE_CODE_MESSAGING_SOCKET": str(sock)}
        started(hook, "sid-old", **env)
        started(hook, "sid-new", source="clear", **env)
        assert hook.to("/session/end") == [], hook.sent()
        assert not list(hook.run_dir.glob("qb-pane-*"))
    finally:
        stranger.kill()
        stranger.wait()


def test_an_older_qb_env_costs_the_pane_and_nothing_else(hook):
    """`qb-hook` and `qb-env` are separately pinned store paths (#204), so a
    half-migrated install can pair this hook with a library that predates the
    pane. That loses the pane in silence and keeps everything else — including
    the session's identity, which is why the key derivation stayed in this file
    rather than moving to the library beside it."""
    (hook.bin / "qb-env").write_text(
        "qb_load_config() { QUARTERBACK_BASE_URL=http://board.test; QUARTERBACK_AGENT=testbox; }\n"
        "qb_resolve_token() { QUARTERBACK_TOKEN=tok-test; return 0; }\n"
    )
    pane = Pane(hook.root)
    env = {**HOOK_ENV, "CLAUDE_CODE_MESSAGING_SOCKET": str(pane.path),
           "CLAUDE_CODE_SESSION_ID": "sid-old"}
    got = hook.fire("SessionStart", env=hook.env(**env),
                    session_id="sid-old", source="startup")
    assert got.stderr == "", got.stderr
    assert any("X-Agent-Instance: sid-old" in c for c in hook.sent()), hook.sent()
    assert not list(hook.run_dir.glob("qb-pane-*"))


# ------------------------------------- the case the pane cannot cover, and does


def test_a_pinned_seat_restarted_in_a_NEW_pane_still_supersedes(hook):
    """The instance-keyed record is not dead code and this is what it is for. An
    operator who pins `QUARTERBACK_INSTANCE` is saying "this identity outlives the
    process", and a restart gives a new CLI — so the pane is new and empty while
    the identity is the same. The pane is read first and this is the fallback."""
    first = Pane(hook.root, name="socks-a", mtime=1_700_000_000)
    second = Pane(hook.root, name="socks-b", mtime=1_700_000_500)
    pinned = {"QUARTERBACK_INSTANCE": "seat-3"}

    started(hook, "sid-old", CLAUDE_CODE_MESSAGING_SOCKET=str(first.path), **pinned)
    started(hook, "sid-new", source="clear",
            CLAUDE_CODE_MESSAGING_SOCKET=str(second.path), **pinned)

    ended = hook.to("/session/end")
    assert len(ended) == 1, hook.sent()
    assert '"session":"sid-old"' in ended[0]
    assert '"reason":"context_reset"' in ended[0]
