"""Tests for worktree-holder and the two scripts that consult it.

The behaviour worth pinning down is the *union*: a lease records the directory
an agent was launched in, which for the worktree workflow is the main checkout,
so the board alone reports every agent as "on main in ~/src/proj" no matter
which worktree it was handed. The session marker supplies the missing half. A
test that only exercised the lease-cwd path would pass while the tool answered
"free" about every worktree in the fleet.

The other half is the failure modes: no board, unreachable board, stale marker.
Each must be distinguishable from "nobody is there", because a caller that
treats "I could not ask" as "the coast is clear" reintroduces the exact silent
rewrite this exists to prevent.

Run: pytest harness/tests
"""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
HOLDER = BIN / "worktree-holder"

MINE = "11111111-1111-1111-1111-111111111111"
PEER = "22222222-2222-2222-2222-222222222222"
DEAD = "33333333-3333-3333-3333-333333333333"


def lease(session, holder="zeus/ember-marten", cwd="/home/x/src/proj", title="fixing 43"):
    """A live agent as GET /active reports one — cwd is the LAUNCH dir."""
    return {
        "session": session,
        "holder": holder,
        "device": "zeus",
        "cwd": cwd,
        "repo": "proj",
        "branch": "main",
        "title": title,
        "model": "claude-opus-5",
        "since": "2026-08-15T13:07:26.534151+00:00",
        "expires": "2026-08-15T23:07:26.534151+00:00",
        "own": False,
    }


def subagent(parent, cwd, holder="zeus/ember-marten"):
    return {
        "parent_session": parent,
        "agent_id": "a1",
        "label": "Explore: panel",
        "cwd": cwd,
        "device": "zeus",
        "holder": holder,
        "since": "2026-08-15T13:07:26.534151+00:00",
        "expires": "2026-08-15T23:07:26.534151+00:00",
    }


class Board:
    """A stub board serving GET /active, recording what it was asked."""

    def __init__(self, payload):
        self.payload = payload
        self.auth_seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.auth_seen.append(self.headers.get("Authorization"))
                body = json.dumps(outer.payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # keep pytest output readable
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def board():
    made = []

    def make(agents=(), subagents=()):
        b = Board({"agents": list(agents), "subagents": list(subagents)})
        made.append(b)
        return b

    yield make
    for b in made:
        b.stop()


@pytest.fixture
def wt(tmp_path):
    """A worktree-shaped directory on a branch, plus an empty marker dir."""
    d = tmp_path / "proj-fix-issue-43"
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "fix/issue-43", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "T"], check=True)
    (tmp_path / "markers").mkdir()
    return d


def run(target, *, board_url=None, markers=None, session=MINE, args=(), token="tok"):
    env = dict(os.environ)
    env.pop("QUARTERBACK_BASE_URL", None)
    env.pop("QUARTERBACK_TOKEN", None)
    # A config file that does not exist is the "unconfigured host" case.
    env["QUARTERBACK_CONFIG"] = "/nonexistent/quarterback/config"
    if board_url:
        env["QUARTERBACK_BASE_URL"] = board_url
        env["QUARTERBACK_TOKEN"] = token
    if markers:
        env["QB_SESSION_CWD_DIR"] = str(markers)
    if session:
        env["CLAUDE_CODE_SESSION_ID"] = session
    else:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    return subprocess.run(
        [str(HOLDER), *args, str(target)], capture_output=True, text=True, env=env
    )


def mark(markers, session, path):
    (markers / session).write_text(f"{path}\n")


# ---------------------------------------------------------------- the union


def test_marked_worktree_held_by_a_live_peer_is_reported(wt, board):
    """The case the board cannot answer alone: the peer's lease says 'main'."""
    b = board(agents=[lease(PEER)])
    mark(wt.parent / "markers", PEER, wt)
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 3
    assert "zeus/ember-marten" in r.stderr
    assert "fix/issue-43" in r.stderr, "should name the worktree's own branch"
    assert "fixing 43" in r.stderr


def test_my_own_marker_is_not_a_holder(wt, board):
    b = board(agents=[lease(MINE)])
    mark(wt.parent / "markers", MINE, wt)
    r = run(wt, board_url=b.url, markers=wt.parent / "markers", session=MINE)
    assert r.returncode == 0
    assert "nobody else" in r.stdout


def test_stale_marker_without_a_live_lease_is_not_a_holder(wt, board):
    """Markers are never cleaned up, so most of them name finished sessions."""
    b = board(agents=[lease(PEER)])
    mark(wt.parent / "markers", DEAD, wt)
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 0


def test_marker_for_a_different_worktree_is_not_a_holder(wt, board):
    b = board(agents=[lease(PEER)])
    mark(wt.parent / "markers", PEER, wt.parent / "proj-fix-issue-99")
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 0


def test_lease_cwd_inside_the_worktree_is_a_holder(wt, board):
    """An agent started in the worktree does report it — no marker needed."""
    b = board(agents=[lease(PEER, cwd=str(wt))])
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 3
    assert "zeus/ember-marten" in r.stderr


def test_subagent_in_the_worktree_is_a_holder(wt, board):
    b = board(subagents=[subagent(PEER, str(wt))])
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 3
    assert "sub-agent" in r.stderr


def test_my_own_subagent_is_not_a_holder(wt, board):
    """A fan-out is not a collision — the board makes the same distinction."""
    b = board(subagents=[subagent(MINE, str(wt))])
    r = run(wt, board_url=b.url, markers=wt.parent / "markers", session=MINE)
    assert r.returncode == 0


def test_empty_board_is_free(wt, board):
    b = board()
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 0


def test_no_session_id_means_every_holder_counts(wt, board):
    """From a plain shell there is no 'me', so nothing may be excluded as mine."""
    b = board(agents=[lease(MINE, cwd=str(wt))])
    r = run(wt, board_url=b.url, markers=wt.parent / "markers", session=None)
    assert r.returncode == 3


# ------------------------------------------------------- resolving the target


def test_branch_name_resolves_to_its_worktree(tmp_path, board):
    """`worktree-holder fix/issue-43` finds the directory git checked it out in."""
    main = tmp_path / "proj"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "T"], check=True)
    (main / "f").write_text("x\n")
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True)
    linked = tmp_path / "proj-fix-issue-43"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "-b", "fix/issue-43", str(linked)],
        check=True,
    )
    markers = tmp_path / "markers"
    markers.mkdir()
    mark(markers, PEER, linked)
    b = board(agents=[lease(PEER)])

    env = dict(os.environ)
    env.update(
        QUARTERBACK_CONFIG="/nonexistent/quarterback/config",
        QUARTERBACK_BASE_URL=b.url,
        QUARTERBACK_TOKEN="tok",
        QB_SESSION_CWD_DIR=str(markers),
        CLAUDE_CODE_SESSION_ID=MINE,
    )
    r = subprocess.run(
        [str(HOLDER), "fix/issue-43"], cwd=str(main), capture_output=True, text=True, env=env
    )
    assert r.returncode == 3, r.stderr
    assert str(linked) in r.stderr


def test_unknown_target_is_an_error_not_an_all_clear(tmp_path, board):
    b = board()
    r = run(tmp_path / "no-such-dir", board_url=b.url)
    assert r.returncode == 2
    assert "No worktree found" in r.stderr


# --------------------------------------------------- could-not-tell is not free


def test_no_board_configured_exits_4(wt):
    r = run(wt, markers=wt.parent / "markers")
    assert r.returncode == 4
    assert "no board configured" in r.stderr


def test_unreachable_board_exits_4(wt, board):
    b = board()
    url = b.url
    b.stop()
    r = run(wt, board_url=url, markers=wt.parent / "markers")
    assert r.returncode == 4
    assert "unreachable" in r.stderr


def test_could_not_tell_says_so_in_json_too(wt):
    r = run(wt, markers=wt.parent / "markers", args=("--json",))
    assert r.returncode == 4
    out = json.loads(r.stdout)
    assert out["checked"] is False
    assert out["held"] is False
    assert out["reason"]


# ------------------------------------------------------------- output contract


def test_json_reports_holders_and_the_worktree_branch(wt, board):
    b = board(agents=[lease(PEER)])
    mark(wt.parent / "markers", PEER, wt)
    r = run(wt, board_url=b.url, markers=wt.parent / "markers", args=("--json",))
    out = json.loads(r.stdout)
    assert out["checked"] is True
    assert out["held"] is True
    assert out["branch"] == "fix/issue-43"
    assert [h["holder"] for h in out["holders"]] == ["zeus/ember-marten"]
    # The lease branch is the launch dir's branch and would say "main" here.
    assert "branch" not in out["holders"][0]


def test_the_all_clear_goes_to_stdout_and_the_warning_to_stderr(wt, board):
    """Callers drop stdout to speak up only when there is a holder."""
    b = board(agents=[lease(PEER)])
    free = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert free.stdout.strip() and not free.stderr.strip()

    mark(wt.parent / "markers", PEER, wt)
    held = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert held.stderr.strip() and not held.stdout.strip()


def test_quiet_prints_nothing_but_still_answers(wt, board):
    b = board(agents=[lease(PEER)])
    mark(wt.parent / "markers", PEER, wt)
    r = run(wt, board_url=b.url, markers=wt.parent / "markers", args=("--quiet",))
    assert r.returncode == 3
    assert r.stdout == "" and r.stderr == ""


def test_the_bearer_token_is_sent(wt, board):
    b = board()
    run(wt, board_url=b.url, markers=wt.parent / "markers", token="sekret")
    assert b.auth_seen == ["Bearer sekret"]


def test_environment_beats_the_config_file(wt, board, tmp_path):
    """Sourcing the site config must not clobber a per-invocation override."""
    b = board(agents=[lease(PEER, cwd=str(wt))])
    cfg = tmp_path / "config"
    cfg.write_text("QUARTERBACK_BASE_URL=http://127.0.0.1:1\nQUARTERBACK_TOKEN_CMD=\"printf wrong\"\n")
    env = dict(os.environ)
    env.update(
        QUARTERBACK_CONFIG=str(cfg),
        QUARTERBACK_BASE_URL=b.url,
        QUARTERBACK_TOKEN="right",
        QB_SESSION_CWD_DIR=str(wt.parent / "markers"),
        CLAUDE_CODE_SESSION_ID=MINE,
    )
    r = subprocess.run([str(HOLDER), str(wt)], capture_output=True, text=True, env=env)
    assert r.returncode == 3, r.stderr
    assert b.auth_seen == ["Bearer right"]


def test_an_environment_token_command_beats_the_config_files(wt, board, tmp_path):
    """TOKEN_CMD is a credential source, not a preference: if the file wins it,
    the query goes out as somebody else."""
    b = board(agents=[lease(PEER, cwd=str(wt))])
    cfg = tmp_path / "config"
    cfg.write_text(f'QUARTERBACK_BASE_URL={b.url}\nQUARTERBACK_TOKEN_CMD="printf from-file"\n')
    env = dict(os.environ)
    env.pop("QUARTERBACK_TOKEN", None)
    env.update(
        QUARTERBACK_CONFIG=str(cfg),
        QUARTERBACK_TOKEN_CMD="printf from-env",
        QB_SESSION_CWD_DIR=str(wt.parent / "markers"),
        CLAUDE_CODE_SESSION_ID=MINE,
    )
    r = subprocess.run([str(HOLDER), str(wt)], capture_output=True, text=True, env=env)
    assert r.returncode == 3, r.stderr
    assert b.auth_seen == ["Bearer from-env"]


def test_the_config_file_supplies_the_board_when_the_environment_does_not(wt, board, tmp_path):
    b = board(agents=[lease(PEER, cwd=str(wt))])
    cfg = tmp_path / "config"
    cfg.write_text(f"QUARTERBACK_BASE_URL={b.url}\nQUARTERBACK_TOKEN_CMD=\"printf from-cmd\"\n")
    env = dict(os.environ)
    env.pop("QUARTERBACK_BASE_URL", None)
    env.pop("QUARTERBACK_TOKEN", None)
    env.update(
        QUARTERBACK_CONFIG=str(cfg),
        QB_SESSION_CWD_DIR=str(wt.parent / "markers"),
        CLAUDE_CODE_SESSION_ID=MINE,
    )
    r = subprocess.run([str(HOLDER), str(wt)], capture_output=True, text=True, env=env)
    assert r.returncode == 3, r.stderr
    assert b.auth_seen == ["Bearer from-cmd"]


# ------------------------------------------------------------- the callers


def _repo_with_leftover(tmp_path, leftover_name):
    """A repo plus a de-registered `<project>-*` sibling that looks like a worktree."""
    main = tmp_path / "proj"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "T"], check=True)
    (main / "f").write_text("x\n")
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True)
    left = tmp_path / leftover_name
    left.mkdir()
    (left / ".git").write_text("gitdir: /gone\n")  # the remnant marker
    return main, left


def _caller_env(board_url, markers, path_extra):
    env = dict(os.environ)
    env.update(
        PATH=f"{path_extra}:{env['PATH']}",
        QUARTERBACK_CONFIG="/nonexistent/quarterback/config",
        QUARTERBACK_BASE_URL=board_url,
        QUARTERBACK_TOKEN="tok",
        QB_SESSION_CWD_DIR=str(markers),
        CLAUDE_CODE_SESSION_ID=MINE,
    )
    return env


def test_prune_leaves_a_held_directory_alone(tmp_path, board):
    """`--remove-dirs` runs rm -rf; a live agent in the directory outranks it."""
    main, left = _repo_with_leftover(tmp_path, "proj-fix-issue-43")
    markers = tmp_path / "markers"
    markers.mkdir()
    mark(markers, PEER, left)
    b = board(agents=[lease(PEER)])

    r = subprocess.run(
        [str(BIN / "prune-worktrees"), "--remove-dirs", "--project", "proj"],
        cwd=str(main), capture_output=True, text=True,
        env=_caller_env(b.url, markers, str(BIN)),
    )
    assert left.is_dir(), "a held worktree must survive --remove-dirs"
    assert "Held by a live agent" in r.stdout
    assert "Leftover directories: none" in r.stdout


def test_prune_still_removes_an_unheld_leftover(tmp_path, board):
    """The guard must not turn the sweeper into a no-op."""
    main, left = _repo_with_leftover(tmp_path, "proj-fix-issue-43")
    markers = tmp_path / "markers"
    markers.mkdir()
    b = board(agents=[lease(PEER)])

    subprocess.run(
        [str(BIN / "prune-worktrees"), "--remove-dirs", "--project", "proj"],
        cwd=str(main), capture_output=True, text=True,
        env=_caller_env(b.url, markers, str(BIN)),
    )
    assert not left.exists()


def test_remove_worktree_refuses_a_held_worktree(tmp_path, board):
    main = tmp_path / "proj"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "T"], check=True)
    (main / "f").write_text("x\n")
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True)
    linked = tmp_path / "proj-fix-issue-43"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "-b", "fix/issue-43", str(linked)],
        check=True,
    )
    markers = tmp_path / "markers"
    markers.mkdir()
    mark(markers, PEER, linked)
    b = board(agents=[lease(PEER)])

    r = subprocess.run(
        [str(BIN / "remove-worktree"), "fix-issue-43"],
        cwd=str(main), capture_output=True, text=True,
        env=_caller_env(b.url, markers, str(BIN)),
    )
    assert r.returncode != 0
    assert linked.is_dir(), "refusal must happen before anything is destroyed"
    assert "zeus/ember-marten" in r.stderr
    assert "--force" in r.stderr


def test_remove_worktree_force_overrides_the_holder(tmp_path, board):
    """Advisory means advisory: --force is always available."""
    main = tmp_path / "proj"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "T"], check=True)
    (main / "f").write_text("x\n")
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True)
    linked = tmp_path / "proj-fix-issue-43"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", "-b", "fix/issue-43", str(linked)],
        check=True,
    )
    markers = tmp_path / "markers"
    markers.mkdir()
    mark(markers, PEER, linked)
    b = board(agents=[lease(PEER)])

    subprocess.run(
        [str(BIN / "remove-worktree"), "--force", "fix-issue-43"],
        cwd=str(main), capture_output=True, text=True,
        env=_caller_env(b.url, markers, str(BIN)),
    )
    assert not linked.exists()


def test_callers_proceed_when_the_holder_check_is_not_installed(tmp_path, board):
    """The harness must work with no board and no helper — degrade, never block."""
    main, left = _repo_with_leftover(tmp_path, "proj-fix-issue-43")
    lone = tmp_path / "lonely-bin"
    lone.mkdir()
    shutil.copy(BIN / "prune-worktrees", lone / "prune-worktrees")
    env = dict(os.environ)
    env["QUARTERBACK_CONFIG"] = "/nonexistent/quarterback/config"
    env.pop("QUARTERBACK_BASE_URL", None)
    env.pop("QUARTERBACK_TOKEN", None)
    subprocess.run(
        [str(lone / "prune-worktrees"), "--remove-dirs", "--project", "proj"],
        cwd=str(main), capture_output=True, text=True, env=env,
    )
    assert not left.exists()


def test_create_worktree_names_the_holder_of_a_path_it_refuses(tmp_path, board):
    """create-worktree runs under `set -e`, so the informational call must not
    abort the refusal it was added to explain."""
    main, occupied = _repo_with_leftover(tmp_path, "proj-fix-issue-43")
    markers = tmp_path / "markers"
    markers.mkdir()
    mark(markers, PEER, occupied)
    b = board(agents=[lease(PEER)])

    r = subprocess.run(
        [str(BIN / "create-worktree"), "--no-fetch", "--shared-db", "fix/issue-43"],
        cwd=str(main), capture_output=True, text=True,
        env=_caller_env(b.url, markers, str(BIN)),
    )
    assert r.returncode != 0
    assert "already exists" in r.stderr
    assert "zeus/ember-marten" in r.stderr


def test_a_symlinked_path_still_matches_its_marker(tmp_path, board):
    """A checkout reached through a symlink must not read as unoccupied."""
    real = tmp_path / "real" / "proj-fix-issue-43"
    real.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "fix/issue-43", str(real)], check=True)
    link = tmp_path / "proj-fix-issue-43"
    link.symlink_to(real)
    markers = tmp_path / "markers"
    markers.mkdir()
    b = board(agents=[lease(PEER)])

    # Marker written as the physical path, queried through the symlink…
    mark(markers, PEER, real)
    assert run(link, board_url=b.url, markers=markers).returncode == 3
    # …and the other way round.
    mark(markers, PEER, link)
    assert run(link, board_url=b.url, markers=markers).returncode == 3


def test_a_lease_with_no_title_still_reports_the_right_fields(wt, board):
    """The holder line is read back with `IFS` set to the field separator, and
    tab is an IFS *whitespace* character — so bash collapsed the two tabs around
    an empty title into one and every field after it shifted left. The title held
    the timestamp, `since` held the literal word "agent" (which `age_of` then
    printed verbatim, having failed to parse it as a date), and the "(sub-agent)"
    marker disappeared.

    A lease with no title is not exotic: the board's `title` is optional and jq
    emits `""` for it. Unit Separator is not IFS whitespace, so empty fields stay
    empty and stay put."""
    b = board(agents=[lease(PEER, cwd=str(wt), title="")])
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")

    assert r.returncode == 3
    assert "zeus/ember-marten" in r.stderr
    # The timestamp must not be quoted back as if the agent had named its task.
    assert '"2026-08-15T13:07:26' not in r.stderr
    # `since` is still a date, so the age renders as an age rather than a word.
    assert "held for" in r.stderr and "held for agent" not in r.stderr


def test_a_subagent_with_no_title_keeps_its_marker(wt, board):
    """A sub-agent entry carries no `title` at all, and the marker that says it is
    a fan-out rather than a peer is the LAST field — the one a field shift drops
    first. This passes on the old code too, so it is a guard rather than a
    regression test; the shift itself is pinned by the lease case above."""
    b = board(subagents=[subagent(PEER, str(wt))])
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 3
    assert "(sub-agent)" in r.stderr


def test_an_agent_launched_inside_the_worktree_is_a_holder(wt, board):
    """`here()` compared a lease `cwd` to the worktree root by string equality,
    so an agent launched from a SUBDIRECTORY — `src/`, `harness/`, anywhere below
    the root, which is the ordinary case — was not reported at all, and
    `remove-worktree` would delete the checkout out from under it. A false
    negative, which is the one answer this tool must never give quietly."""
    b = board(agents=[lease(PEER, cwd=str(wt / "harness" / "loops"))])
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 3
    assert "zeus/ember-marten" in r.stderr


def test_a_sibling_sharing_a_path_prefix_is_not_a_holder(wt, board):
    """The boundary the separator buys: `…/proj-fix-issue-4` must not match
    `…/proj-fix-issue-43`. Prefix matching without it turns a false negative into
    a false positive, which blocks work on a worktree nobody is in."""
    b = board(agents=[lease(PEER, cwd=str(wt) + "-and-more")])
    r = run(wt, board_url=b.url, markers=wt.parent / "markers")
    assert r.returncode == 0


def test_a_held_worktree_is_protected_from_every_sweep_not_just_the_directory(
        tmp_path, board):
    """The held-worktree guard used to divert only the DIRECTORY into HELD_DIRS,
    while the database, port, nginx and container sweeps each derived
    orphan-ness independently from `git worktree list`. So a de-registered but
    HELD worktree was still reported as an orphan database and had DROP DATABASE
    run on it, still had its port entry rewritten away and the port
    reallocated, still had its nginx block stripped — and, because DEAD_SUFFIX is
    seeded from the stale-port list as well as the leftover dirs, still had
    `docker rm -f` run on its containers. All under a live agent, and all while
    the report said the directory was being left alone.

    A held worktree is registered as LIVE now, so every sweep inherits the
    protection — including any sweep added later, since they all already ask
    "is this live?". The port entry is the one this test can drive without a
    database or a docker daemon."""
    main, left = _repo_with_leftover(tmp_path, "proj-fix-issue-43")
    (main / ".worktree-ports").write_text("8091:fix/issue-43\n8092:gone-for-good\n")
    markers = tmp_path / "markers"
    markers.mkdir()
    mark(markers, PEER, left)
    b = board(agents=[lease(PEER)])

    r = subprocess.run(
        [str(BIN / "prune-worktrees"), "--project", "proj"],
        cwd=str(main), capture_output=True, text=True,
        env=_caller_env(b.url, markers, str(BIN)),
    )

    assert "Held by a live agent" in r.stdout
    # The held worktree's port entry is not stale — something is using it.
    assert "8091:fix/issue-43" not in r.stdout, "a held worktree's port was reclaimed"
    # …and the genuinely dead one still is, or the guard has swallowed the sweep.
    assert "8092:gone-for-good" in r.stdout


# ------------------------------------------------- the markers, subtractively


@pytest.fixture
def main_checkout(tmp_path):
    """The MAIN checkout — the directory every agent's lease reports as its cwd."""
    d = tmp_path / "proj"
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "T"], check=True)
    (tmp_path / "markers").mkdir(exist_ok=True)
    return d


def test_a_peer_marked_for_another_worktree_does_not_hold_the_main_checkout(
    main_checkout, tmp_path, board
):
    """The markers have to answer subtractively, and using them only additively
    left a false positive on the one checkout that matters most.

    A lease records the directory an agent was LAUNCHED in, which for the
    worktree workflow is the main checkout — that is the whole reason the markers
    exist. So `under($p)` is true of EVERY live agent in the repo when $p is the
    main checkout, including ones demonstrably working somewhere else.

    Measured on hermes while #83 was being written: this tool named
    `hermes/seat-quarterback-5` as holding the main checkout, while that
    session's own marker said `…/quarterback-fix-issue-458`. Benign for
    `remove-worktree`, which only ever asks about a linked worktree and for which
    a false positive is a refusal to delete. Not benign for anything that wants
    to act ON the main checkout: #83's catch-up would decline forever on any box
    with an agent running, which is every box.
    """
    markers = tmp_path / "markers"
    mark(markers, PEER, str(tmp_path / "proj-fix-issue-99"))
    b = board(agents=[lease(PEER, cwd=str(main_checkout))])

    done = run(main_checkout, board_url=b.url, markers=markers, args=("--json",))
    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)
    assert answer["held"] is False, (
        f"a session working in proj-fix-issue-99 was reported as holding the "
        f"main checkout: {answer['holders']}")


def test_a_peer_with_no_marker_still_holds_the_checkout_it_was_launched_in(
    main_checkout, tmp_path, board
):
    """The true positive the subtraction must never drop: an agent started
    directly inside a checkout has no marker at all, and its lease cwd is the
    only evidence there is."""
    markers = tmp_path / "markers"
    b = board(agents=[lease(PEER, cwd=str(main_checkout))])

    done = run(main_checkout, board_url=b.url, markers=markers, args=("--json",))
    assert done.returncode == 3, f"an unmarked agent launched here was not seen: {done.stdout}"
    answer = json.loads(done.stdout)
    assert [h["session"] for h in answer["holders"]] == [PEER]


def test_a_peer_marked_for_this_worktree_holds_it_wherever_it_was_launched(
    wt, tmp_path, board
):
    """The additive half, unchanged: the marker is what says an agent is in a
    worktree its lease knows nothing about."""
    markers = tmp_path / "markers"
    mark(markers, PEER, str(wt))
    b = board(agents=[lease(PEER, cwd="/home/x/src/proj")])

    done = run(wt, board_url=b.url, markers=markers, args=("--json",))
    assert done.returncode == 3, done.stdout
    assert [h["session"] for h in json.loads(done.stdout)["holders"]] == [PEER]
