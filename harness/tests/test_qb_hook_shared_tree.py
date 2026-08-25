"""The shared-checkout guard: `qb-hook` refusing a destructive git verb (#185).

This is the only place on the board that says *no* to anything. Every other
signal here is advisory because an advisory signal is enough — you can always
act on it later. This one cannot be, and that asymmetry is the whole argument
for it: `git reset --hard` in a tree holding a peer's uncommitted work destroys
that work at the instant it runs, with no later moment at which a warning would
still have helped.

It has happened five times on this fleet. Four in 65lowther (#185's evidence:
boards 3860, 3879, 4004) and twice on 2026-08-25 in quarterback's own shared
checkout, where a reset took a peer's in-flight review fixes (boards 6236, 6241).

#185 proposes gating "the first write to a path". The tests here exist partly to
record why that is not what was built: not one of those five went through
`Edit`/`Write`. They were git subcommands in `Bash`, which is a different hook
event with a different matcher, and an `Edit`/`Write` gate would not have been
late — it would never have fired at all.

Three facts have to be true together before anything is refused, and each of them
has a test below saying what happens when it is not:

  1. the command destroys uncommitted work    (`git status` sails through)
  2. this tree actually has some              (a clean tree is never refused)
  3. a peer is live in this exact cwd         (alone in a tree, do as you like)

Plus the two properties that keep a refusal from becoming a wall: the escape
hatch, and failing open when the board cannot be reached.

`git stash` is the one member of this family the guard leaves alone, and there is
a test pinning that. It is already refused by the `reference-transaction` hook
`qb-hooks` installs, which is strictly better for it — that one catches a stash
typed outside Claude Code, and it does not wait for a peer to be live, because a
shared `refs/stash` stack is a hazard either way. Two gates on one command would
only mean two escape hatches under different names.

These drive the real script as a subprocess against a stub board — the same
technique as `test_qb_hook_end.py`, and for the same reason: asserting on the
text of the file cannot tell a branch that is present from one that runs.

Run: pytest harness/tests
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from urllib.parse import quote
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
HOOK = BIN / "qb-hook"
CLASSIFY = BIN / "qb-classify-command"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None or shutil.which("git") is None,
    reason="qb-hook is bash, parses its payload with jq, and this guard asks git",
)

# One live peer, shaped like `GET /active` really answers. `holder` is the field
# the guard names in its refusal — it is the address you reply to, which is the
# entire point of naming anyone at all (#172: a refusal should be somebody to
# talk to, not a wall).
PEER = {
    "agents": [{"holder": "hermes/seat-quarterback-4", "cwd": "/shared", "own": False}],
    "subagents": [],
}
ALONE: dict = {"agents": [], "subagents": []}


class Guarded:
    """`qb-hook` with a stub board, a stub curl, and a real git checkout as cwd.

    The checkout has to be real: the guard asks git whether the tree is dirty,
    and a fake would make the second of the three facts untestable.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.bin = tmp_path / "hookbin"
        self.bin.mkdir()
        (self.bin / "qb-hook").write_bytes(HOOK.read_bytes())
        (self.bin / "qb-hook").chmod(0o755)
        # The classifier lives beside the hook, found the same way `qb-env` is.
        # Leaving it out of this copy is not a neutral omission — the guard reads
        # an absent classifier as "cannot classify" and lets everything through,
        # so a fixture missing it would run every test below against a hook that
        # refuses nothing and report them all green.
        (self.bin / "qb-classify-command").write_bytes(CLASSIFY.read_bytes())
        (self.bin / "qb-classify-command").chmod(0o755)
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
        self.reply = tmp_path / "active.json"
        self.match = tmp_path / "peer-query.txt"
        self.rc = tmp_path / "curl.rc"
        self.reply.write_text(json.dumps(ALONE))
        self.match.write_text("cwd=\ncwd=\n")   # by default, any tree
        self.rc.write_text("0")
        # THE STUB ANSWERS THE QUESTION IT WAS ASKED. The first version of this
        # returned the peer list for every `/active` call whatever its query, so
        # every test below named "this exact tree" proved only that the regex had
        # matched — the guard could have asked about any path at all and passed.
        # An adversarial review caught that, and it is the reason the target-tree
        # tests further down can mean anything.
        (self.stub / "curl").write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {self.calls}\n'
            f'rc=$(cat {self.rc})\n'
            '[ "$rc" != "0" ] && exit "$rc"\n'
            f'want=$(sed -n 1p {self.match}); raw=$(sed -n 2p {self.match})\n'
            'case "$*" in\n'
            f'  *"/active"*"repo="*) cat {self.reply} ;;\n'
            f'  *"/active"*"$want"*) cat {self.reply} ;;\n'
            f'  *"/active"*"$raw"*) cat {self.reply} ;;\n'
            '  *"/active"*) printf \'{"agents":[],"subagents":[]}\' ;;\n'
            'esac\n'
            "exit 0\n"
        )
        (self.stub / "curl").chmod(0o755)

        self.cwd = tmp_path / "shared"
        self.cwd.mkdir()
        self._git("init", "-q", "-b", "main")
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@test", "-c", "user.name=t", "-C", str(self.cwd), *args],
            check=True, capture_output=True,
        )

    def commit(self, name: str, body: str) -> Path:
        f = self.cwd / name
        f.write_text(body)
        self._git("add", name)
        self._git("commit", "-qm", "seed")
        return f

    def peers(self, payload: dict, in_tree: Path | str | None = None) -> None:
        """Who the board reports, and — optionally — the ONE tree it reports them
        in. `in_tree` is what makes "does the guard ask about the right path?"
        a question the stub can actually answer."""
        self.reply.write_text(json.dumps(payload))
        self.match.write_text(
            f"cwd={quote(str(in_tree), safe='')}\ncwd={in_tree}\n"
            if in_tree is not None else "cwd=\ncwd=\n"
        )

    def other_checkout(self, name: str = "private") -> Path:
        """A second, independent git checkout — the tree a `git -C` points at."""
        d = self.root / name
        d.mkdir()
        subprocess.run(["git", "-C", str(d), "init", "-q", "-b", "main"],
                       check=True, capture_output=True)
        return d

    def board_down(self) -> None:
        self.rc.write_text("7")  # curl's "couldn't connect"

    def env(self, **over) -> dict:
        base = {k: v for k, v in os.environ.items()
                if k not in ("TMUX", "TMUX_PANE", "QUARTERBACK_INSTANCE")}
        return {**base,
                "PATH": f"{self.stub}:{os.environ['PATH']}",
                "XDG_RUNTIME_DIR": str(self.run_dir),
                "HOME": str(self.root / "home"),
                **over}

    def bash(self, command: str) -> subprocess.CompletedProcess:
        return self.fire("PreToolUse", tool_name="Bash", tool_input={"command": command})

    def fire(self, event: str, **payload) -> subprocess.CompletedProcess:
        body = {"session_id": "sid-1", "cwd": str(self.cwd),
                "transcript_path": "", **payload}
        got = subprocess.run([str(self.bin / "qb-hook"), event],
                             input=json.dumps(body), capture_output=True, text=True,
                             env=self.env(), timeout=60)
        # Fail-open is a contract, not an aspiration: whatever this hook decides,
        # it exits 0. A non-zero exit is Claude Code's "the hook is broken".
        assert got.returncode == 0, got.stderr
        return got

    def decision(self, got: subprocess.CompletedProcess) -> dict | None:
        """The permission decision, or None where the call was let through."""
        out = got.stdout.strip()
        if not out:
            return None
        return json.loads(out).get("hookSpecificOutput")

    def sent(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def wait_for(self, fragment: str, seconds: float = 5.0) -> bool:
        """The Task branch fires and forgets its call so the spawn is never
        delayed by board latency, so a test that read the log once would race it.
        The guard's own call is foreground by necessity — it needs the answer."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if any(fragment in c for c in self.sent()):
                return True
            time.sleep(0.05)
        return False


@pytest.fixture
def guard(tmp_path):
    return Guarded(tmp_path)


@pytest.fixture
def shared(guard):
    """The situation on 2026-08-25: a dirty tree with a live peer in it."""
    guard.commit("app.py", "original\n")
    (guard.cwd / "app.py").write_text("a peer's in-flight edit\n")
    guard.peers(PEER, in_tree=guard.cwd)
    return guard


# ------------------------------------------------------- the refusal itself


def test_a_hard_reset_in_a_shared_dirty_tree_is_refused(shared):
    """Board 6236/6241, exactly: the command that destroyed the work, in the tree
    it destroyed it in, with the agent whose work it was still live there."""
    d = shared.decision(shared.bash("git reset --hard origin/main"))
    assert d is not None, "the reset was allowed through"
    assert d["permissionDecision"] == "deny"


def test_the_refusal_names_the_peer_to_talk_to(shared):
    """A refusal that does not name anyone is a wall. Naming the holder is what
    turns it into the conversation that should have happened first."""
    d = shared.decision(shared.bash("git reset --hard"))
    assert "hermes/seat-quarterback-4" in d["permissionDecisionReason"]


def test_the_refusal_says_what_to_do_instead(shared):
    """The agent has a real need — it wants a clean tree. Refusing without a way
    to get one is how a gate gets worked around instead of used."""
    reason = shared.decision(shared.bash("git reset --hard"))["permissionDecisionReason"]
    assert "git worktree add" in reason
    # `qb-stash push`, not `git stash`: every worktree of a repo shares one
    # `refs/stash`, and the harness installs a hook that refuses the shared one
    # outright — so advice to `git stash -u` sent the reader into a second
    # refusal from the same fleet.
    assert "qb-stash push" in reason
    assert "git stash" not in reason


def test_a_peers_subagent_counts_as_a_peer(shared):
    """A peer's fan-out edits the peer's tree with the peer's hands. Losing its
    work loses the peer's work, so `subagents` is not a separate, softer case."""
    shared.peers({"agents": [], "subagents": [{"label": "Explore: audit"}]}, in_tree=shared.cwd)
    d = shared.decision(shared.bash("git clean -fd"))
    assert d["permissionDecision"] == "deny"
    assert "Explore: audit" in d["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [
    "git reset --hard",
    "git reset --merge",
    "git checkout -- .",
    "git checkout HEAD -- app.py",
    "git checkout -f main",
    "git restore app.py",
    "git clean -fd",
    "git clean --force",
    "git worktree remove --force ../wt",
    "cd /tmp && git reset --hard",              # not at the start of the line
    "git switch -f main",                       # the modern spelling of checkout -f
    "git switch --discard-changes main",
])
def test_the_verbs_that_destroy_a_peers_uncommitted_work(shared, cmd):
    assert shared.decision(shared.bash(cmd))["permissionDecision"] == "deny", cmd


@pytest.mark.parametrize("cmd", [
    "git status",
    "git diff",
    "git reset HEAD~1",                          # mixed reset keeps the worktree
    "git reset --soft HEAD~1",
    "git checkout -b feat/x",
    "git checkout main",
    "git checkout main && git status",           # the match must not run past &&
    "git stash",                                 # guarded one layer down, by qb-hooks
    "git stash push -u",
    "git stash list",
    "git clean -n",
    "git clean --dry-run",                       # `--d` is not the `-d` flag
    "git worktree remove ../wt",                 # without --force, git refuses on its own
    "grep -rn 'git reset --hard' harness/",      # talking about it is not doing it
])
def test_what_is_not_refused(shared, cmd):
    """The gate lives on the hottest path in the hook — every Bash call in every
    session on the box. A false positive here is not a cosmetic problem: it is
    the thing that gets the whole guard turned off."""
    assert shared.decision(shared.bash(cmd)) is None, cmd


# ------------------------------------------- the three facts, taken away one at a time


def test_a_clean_tree_is_never_refused(guard):
    """Nothing uncommitted, nothing to lose. Refusing here would be the gate
    crying wolf on the safest possible instance of the verb it watches."""
    guard.commit("app.py", "committed and clean\n")
    guard.peers(PEER, in_tree=guard.cwd)
    assert guard.decision(guard.bash("git reset --hard")) is None


def test_an_untracked_file_counts_as_something_to_lose(guard):
    """Board 4004 was an untracked file — a half-written include that became
    everyone's red build. `git clean -fd` destroys precisely the files that
    `--untracked-files=no` would have hidden from this check."""
    (guard.cwd / "fitout.yaml").write_text("half-written\n")
    guard.peers(PEER, in_tree=guard.cwd)
    assert guard.decision(guard.bash("git clean -fd"))["permissionDecision"] == "deny"


def test_alone_in_a_dirty_tree_you_may_do_as_you_like(guard):
    """Your own uncommitted work is yours to throw away. The guard is about other
    people's, and with nobody else here there is nobody else's."""
    guard.commit("app.py", "original\n")
    (guard.cwd / "app.py").write_text("my own scratch\n")
    guard.peers(ALONE)
    assert guard.decision(guard.bash("git reset --hard")) is None


def test_a_harmless_command_never_reaches_the_board(shared):
    """The fast path, and the reason it is a regex before it is anything else.
    This fires on every Bash call in every session; if an ordinary `git status`
    cost a round trip, the guard's cost would be the fleet's cost."""
    shared.bash("git status")
    assert [c for c in shared.sent() if "/active" in c] == []


def test_the_peer_check_asks_about_this_tree_not_this_repo(shared):
    """Two agents in two worktrees of one repo are not in each other's way. A
    gate that refused them would be refusing people who are free — which is the
    failure #185 warns about for the path key, in a different spelling."""
    shared.bash("git reset --hard")
    active = [c for c in shared.sent() if "/active" in c]
    assert len(active) == 1, shared.sent()
    assert "cwd=" in active[0]
    assert "repo=" not in active[0]


def test_our_own_session_is_not_our_own_collision(shared):
    """`peers_only` needs `mine` to know which lease is ours. Without it this
    session's own lease comes back as a peer and the guard refuses everyone,
    forever, starting with itself."""
    shared.bash("git reset --hard")
    active = [c for c in shared.sent() if "/active" in c][0]
    assert "peers_only=true" in active
    assert "mine=sid-1" in active


# ------------------------------------------------------- not a wall, not a lock


def test_the_escape_hatch_lets_a_settled_disagreement_through(shared):
    """An advisory gate needs a way past or it gets disabled wholesale, and then
    it guards nothing. Putting it in the command makes taking it deliberate and
    visible rather than a shrug."""
    assert shared.decision(shared.bash("QB_ALLOW_SHARED_TREE=1 git reset --hard")) is None


def test_a_board_that_is_down_stops_nobody(shared):
    """The hook's oldest contract. A coordination board is not in the critical
    path of anyone's work, and a guard that failed closed would turn every board
    outage into a fleet-wide outage."""
    shared.board_down()
    assert shared.decision(shared.bash("git reset --hard")) is None


def test_a_cwd_that_is_not_a_checkout_is_not_guarded(guard):
    """Nothing here to be anyone's working tree."""
    outside = guard.root / "elsewhere"
    outside.mkdir()
    guard.peers(PEER, in_tree=guard.cwd)
    got = guard.fire("PreToolUse", cwd=str(outside), tool_name="Bash",
                     tool_input={"command": "git reset --hard"})
    assert guard.decision(got) is None


# ------------------------------------------------------------------ regression


def test_widening_the_matcher_did_not_cost_task_its_bookkeeping(guard):
    """`PreToolUse` was scoped to `Task` alone before this. Bash sharing the event
    must not take sub-agent registration down with it — that is v2.6's, and it is
    how `active` tells a peer's fan-out from a peer."""
    guard.fire("PreToolUse", tool_name="Task",
               tool_input={"subagent_type": "Explore", "description": "look around"})
    assert guard.wait_for("/subagent"), guard.sent()


def test_a_bash_call_does_not_register_a_subagent(shared):
    """The other half of the same seam: the Task branch must not run for Bash."""
    shared.bash("git status")
    assert [c for c in shared.sent() if "/subagent" in c] == []


# --------------------------------------------- the awareness half of the same datum


class Started(Guarded):
    """`SessionStart` in a real checkout, where `repo` is non-empty — which is the
    condition under which the occupancy note has never once asked about a tree."""

    def start(self) -> str:
        got = self.fire("SessionStart")
        out = got.stdout.strip()
        if not out:
            return ""
        return json.loads(out)["hookSpecificOutput"].get("additionalContext", "")


@pytest.fixture
def session(tmp_path):
    g = Started(tmp_path)
    g._git("remote", "add", "origin", "git@github.com:prisonblues/quarterback.git")
    g.commit("app.py", "seed\n")
    return g


def test_a_peer_in_your_tree_is_not_reported_as_a_peer_in_your_repo(session):
    """The bug seat-quarterback-2 found, and the reason it cost a working tree.

    `repo` wins the scope and inside a git repo it always wins — so `cwd` was
    never sent, and the note that named the very agents whose work was destroyed
    minutes later (boards 6236, 6241) described them as sharing a *repo* and
    closed by saying not to hold off. It was answering a different question from
    the one that mattered, in a voice that sounded like it had answered it."""
    session.peers(PEER, in_tree=session.cwd)
    note = session.start()
    assert "SHARING a working tree" in note, note
    assert "hermes/seat-quarterback-4" in note


def test_the_shared_tree_note_says_the_opposite_of_the_repo_note(session):
    """Two overlaps, two instructions. Sharing a repo is company and the note
    says so; sharing a tree is not free and the note must not inherit that
    sentence, which is precisely what it did on the night."""
    session.peers(PEER, in_tree=session.cwd)
    note = session.start()
    shared = note.split("SHARING a working tree", 1)[1]
    assert "no need to hold off" not in shared
    assert "git worktree add" in shared


def test_the_repo_note_keeps_its_own_voice_for_a_peer_elsewhere(session):
    """The fix must not make the ordinary case alarming. A peer in ANOTHER
    worktree of the same repo is what the board is for, and it still reads that
    way — the friendly note, and no warning."""
    session.peers(PEER, in_tree="/somewhere/else")
    note = session.start()
    assert "Working the same area is fine" in note
    assert "SHARING a working tree" not in note


def test_the_tree_note_replaces_the_repo_note_rather_than_arguing_with_it(session):
    """447-F39. A peer sharing BOTH fired both notes, softest first: "no need to
    hold off" immediately above "get out of this tree". One peer, one situation,
    two contradictory instructions — and the wrong one led."""
    session.peers(PEER, in_tree=session.cwd)
    note = session.start()
    assert "SHARING a working tree" in note
    assert "no need to hold off" not in note


def test_alone_in_your_tree_there_is_no_second_note(session):
    """`GET /active?cwd=` answers about this tree; an empty answer is silence,
    not a softer warning."""
    session.peers(ALONE, in_tree=session.cwd)
    assert "SHARING a working tree" not in session.start()


def test_both_questions_are_asked_not_one(session):
    """The repo question and the tree question are different questions and the
    hook now asks both. Collapsing them back into one scope is the bug."""
    session.peers(PEER, in_tree=session.cwd)
    session.start()
    active = [c for c in session.sent() if "/active" in c]
    assert any("repo=" in c for c in active), active
    assert any("cwd=" in c for c in active), active


# ------------------------------------------------- which tree does it actually touch?
#
# The sharpest finding of the adversarial review, and the one the first cut got
# wrong in both directions. The guard used to check the payload `cwd`
# unconditionally, while the command is free to name a different tree — so it
# would let a peer's checkout be destroyed from a clean cwd, and refuse a private
# checkout from a shared one. Neither is a near-miss; they are the two halves of
# checking the wrong thing.


def test_a_reset_aimed_at_a_peers_tree_is_refused_from_a_clean_cwd(guard):
    """`git -C <peer-tree> reset --hard`, run from somewhere quiet. Nothing about
    the cwd is alarming; the tree in the command is somebody's morning."""
    peer_tree = guard.other_checkout("peer")
    (peer_tree / "wip.py").write_text("a peer's in-flight edit\n")
    guard.peers(PEER, in_tree=peer_tree)
    d = guard.decision(guard.bash(f"git -C {peer_tree} reset --hard"))
    assert d is not None and d["permissionDecision"] == "deny"
    assert str(peer_tree) in d["permissionDecisionReason"]


def test_the_work_tree_flag_is_followed_too(guard):
    peer_tree = guard.other_checkout("peer")
    (peer_tree / "wip.py").write_text("in flight\n")
    guard.peers(PEER, in_tree=peer_tree)
    d = guard.decision(guard.bash(f"git --work-tree={peer_tree} --git-dir={peer_tree}/.git reset --hard"))
    assert d is not None and d["permissionDecision"] == "deny"


def test_a_private_tree_is_not_refused_because_the_cwd_is_shared(shared):
    """The mirror image, and the one that would have taught people to ignore the
    guard: standing in a shared tree does not make every command you type
    dangerous. The reset lands on a checkout nobody else is in."""
    private = shared.other_checkout("private")
    (private / "scratch.txt").write_text("mine alone\n")
    assert shared.decision(shared.bash(f"git -C {private} reset --hard")) is None


def test_a_relative_target_is_resolved_against_the_cwd(guard):
    peer_tree = guard.other_checkout("peer")
    (peer_tree / "wip.py").write_text("in flight\n")
    guard.peers(PEER, in_tree=peer_tree)
    d = guard.decision(guard.bash("git -C ../peer reset --hard"))
    assert d is not None and d["permissionDecision"] == "deny"


def test_a_target_we_cannot_resolve_falls_back_to_the_cwd(shared):
    """A quoted path, a `$VAR` or a `$(…)` is not something a regex resolves, and
    guessing at one would be confidently wrong about where the damage lands. The
    honest move is to guard the tree we are standing in and say nothing about the
    other — which is what the first cut did for every command."""
    d = shared.decision(shared.bash('git -C "$SOME_DIR" reset --hard'))
    assert d is not None and d["permissionDecision"] == "deny"


# ----------------------------------------------------- the tree, not the directory


def test_a_peer_one_directory_down_is_still_in_your_tree(guard):
    """#185 says this in as many words — "an agent sitting in 65lowther/viz is in
    the same tree with a different cwd" — and the first cut compared raw cwds, so
    it could not see them. `git reset --hard` from either directory wrecks the
    same working tree."""
    guard.commit("app.py", "original\n")
    (guard.cwd / "app.py").write_text("a peer's edit\n")
    sub = guard.cwd / "subdir"
    sub.mkdir()
    # The peer's lease names the ROOT; we are standing one level down.
    guard.peers(PEER, in_tree=guard.cwd)
    got = guard.fire("PreToolUse", cwd=str(sub), tool_name="Bash",
                     tool_input={"command": "git reset --hard"})
    d = guard.decision(got)
    assert d is not None and d["permissionDecision"] == "deny"


def test_a_peer_whose_lease_names_a_subdirectory_is_found_too(guard):
    """The other direction, and the reason the guard asks twice. A lease records
    whatever cwd its session started in and the board matches that string
    exactly. We can canonicalise our own side; we cannot canonicalise theirs."""
    guard.commit("app.py", "original\n")
    (guard.cwd / "app.py").write_text("my edit\n")
    sub = guard.cwd / "viz"
    sub.mkdir()
    guard.peers(PEER, in_tree=sub)          # the peer started down there
    got = guard.fire("PreToolUse", cwd=str(sub), tool_name="Bash",
                     tool_input={"command": "git reset --hard"})
    assert guard.decision(got)["permissionDecision"] == "deny"


# ------------------------------------------------------------- false positives


@pytest.mark.parametrize("cmd", [
    "git clean -fdn",                 # -n is a dry run however it is clustered
    "git clean -ndf",
    "git clean --dry-run --force",
    "git restore --help",             # asking a verb to explain itself
    "git reset --hard --help",
    "git clean -fd -h",
])
def test_a_command_that_destroys_nothing_is_not_refused(shared, cmd):
    """Each of these was refused by the first cut. A false positive costs more
    here than it looks: this runs on every Bash call in every session, and the
    guard that cries wolf is the guard somebody turns off."""
    assert shared.decision(shared.bash(cmd)) is None, cmd


# --------------------------------------------------------- the escape hatch, exactly


def test_the_hatch_must_be_a_real_leading_assignment(shared):
    """A bare substring test made the hatch quietly wider than the sentence
    documenting it. All three of these used to walk straight through."""
    for cmd in (
        "QB_ALLOW_SHARED_TREE=10 git reset --hard",       # not the value
        "git reset --hard # QB_ALLOW_SHARED_TREE=true",   # a comment, not an assignment
        "echo QB_ALLOW_SHARED_TREE=1; git reset --hard",  # assigns nothing at all
    ):
        d = shared.decision(shared.bash(cmd))
        assert d is not None and d["permissionDecision"] == "deny", cmd


@pytest.mark.parametrize("cmd", [
    "QB_ALLOW_SHARED_TREE=1 git reset --hard",
    "QB_ALLOW_SHARED_TREE=true git clean -fd",
    "  QB_ALLOW_SHARED_TREE=1 git reset --hard",
])
def test_the_documented_spelling_still_works(shared, cmd):
    assert shared.decision(shared.bash(cmd)) is None, cmd


# ------------------------------------------------------------------ hot path


def test_the_git_calls_are_bounded(shared):
    """`curl` has always had `--max-time`; the git calls had nothing, and they now
    sit on the interactive hot path where `git status` can block indefinitely on a
    dead network mount or a slow fsmonitor. Asserted on the source because a test
    that actually hung would be indistinguishable from a slow one."""
    hook = (BIN / "qb-hook").read_text()
    guard = hook.split("_shared_tree_guard() {", 1)[1].split("\n}", 1)[0]
    root = hook.split("_tree_root() {", 1)[1].split("\n}", 1)[0]
    for body in (guard, root):
        for line in body.splitlines():
            if "git -C" in line and not line.strip().startswith("#"):
                assert "timeout" in line, line


# --------------------------------------------------- the note and the gate agree


def test_the_startup_note_counts_a_peers_subagent(session):
    """The guard counts agents AND sub-agents, because a peer's fan-out edits the
    peer's tree with the peer's hands. The note counted only agents, so a tree
    occupied by a sub-agent warned nobody at startup and then refused them at the
    first destructive verb. Two answers to one question."""
    session.peers({"agents": [], "subagents": [{"label": "Explore: audit"}]},
                  in_tree=session.cwd)
    assert "SHARING a working tree" in session.start()


# ------------------------------------- the bypasses, end to end through the hook
#
# `test_qb_classify_command.py` holds the full matrix against the classifier
# itself. These are the same defects driven through the real hook, because the
# classifier being right is only half of it — the hook has to ask it, read its
# answer, and act. The first cut had a correct-looking guard whose regex never
# matched these at all.


def test_a_global_option_with_a_value_no_longer_hides_the_verb(shared):
    """447-F01, end to end. The whole match failed on this, so the guard never
    ran — not the target lookup, the classification."""
    d = shared.decision(shared.bash("git -c core.filemode=false reset --hard"))
    assert d is not None and d["permissionDecision"] == "deny"


def test_a_harmless_clause_no_longer_excuses_a_destructive_one(shared):
    """447-F13/F14, end to end. Both exemptions were tested against the whole
    command string, so a `--help` or a `-n` anywhere in it excused everything."""
    for cmd in ("git clean -n && git reset --hard",
                "git status --help; git reset --hard"):
        d = shared.decision(shared.bash(cmd))
        assert d is not None and d["permissionDecision"] == "deny", cmd


def test_a_hatch_on_another_line_no_longer_excuses_the_reset(shared):
    """447-F15/F17, end to end. grep's `^` is a per-line anchor."""
    d = shared.decision(shared.bash("QB_ALLOW_SHARED_TREE=1 echo hi\ngit reset --hard"))
    assert d is not None and d["permissionDecision"] == "deny"


def test_an_install_without_the_classifier_lets_everything_through(shared):
    """Fail-open, and worth pinning because it is what a half-migrated install
    looks like: `qb-hook` and `qb-classify-command` are separately-pinned store
    paths, so a hook can outrun its own classifier. The guard must then refuse
    nothing rather than guess — and a fixture that lost the classifier silently
    would report every test in this file green against a hook that does nothing,
    which is why this one asserts the behaviour instead of leaving it implied."""
    (shared.bin / "qb-classify-command").unlink()
    assert shared.decision(shared.bash("git reset --hard")) is None


def test_a_commit_dash_a_in_a_shared_tree_is_refused_and_says_the_right_thing(shared):
    """The mechanism in two of #185's five incidents, and the one the verb list
    did not cover at all. It must not be told it "destroys" anything — nothing is
    destroyed when your commit absorbs a peer's in-flight file, and its author
    has still lost it."""
    d = shared.decision(shared.bash("git commit -a -m wip"))
    assert d is not None and d["permissionDecision"] == "deny"
    reason = d["permissionDecisionReason"]
    assert "takes their half-finished work into your commit" in reason
    assert "destroys uncommitted work" not in reason
    assert "3879" in reason


def test_staging_everything_is_refused_and_naming_your_files_is_not(shared):
    assert shared.decision(shared.bash("git add ."))["permissionDecision"] == "deny"
    assert shared.decision(shared.bash("git add -A"))["permissionDecision"] == "deny"
    assert shared.decision(shared.bash("git add app.py")) is None
    assert shared.decision(shared.bash("git commit -m 'only what I staged'")) is None
