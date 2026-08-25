"""`qb-mode` — which way this repo is worked, and whether this tree agrees (#178).

Two ways of working exist in this fleet, both legitimate, and until #178 nothing
named them or showed which one you were in. That cost this repo a wrong
attribution on 2026-08-17 (three agents in one checkout; a `nix build` compiled
one agent's in-progress edits as another's evidence) and two agents' uncommitted
work on 2026-08-25 (four agents in one checkout; a `git reset --hard`). Neither
time did anybody CHOOSE the shared tree — the session started there.

**The resolution is tested next door, and deliberately not again here.**
`test_harness_rules.py` owns the presets, the two axes, the fallbacks and the
alarm's conditions. What this suite owns is the part only a subprocess can show:
the exit codes a caller branches on, the output shapes, and the fact that the
answer survives being asked from somewhere that is not the checkout.

**And the coupling, which is the half a unit test cannot reach.** `qb-hook` reads
this command's JSON with `jq`, by field name, in bash. Rename `violation` and
nothing fails: the note simply stops appearing at session start, which is
indistinguishable from a repo with no problem — the exact silence #178 exists to
end. So the fields the hook names are asserted against the fields the tool emits.

Run: pytest harness/tests/test_qb_mode.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
QB_MODE = HARNESS / "bin" / "qb-mode"
QB_HOOK = HARNESS / "bin" / "qb-hook"
LOOPS = HARNESS / "loops"

AGREES, VIOLATED, CANNOT_TELL = 0, 3, 4

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """`qb-mode`, as a caller runs it.

    XDG_CONFIG_HOME is redirected because the per-box overlay lives outside the
    checkout: without it this suite reads the developer's own
    `~/.config/quarterback/harness-rules.json` and passes or fails on what that
    machine happens to hold, which is the host-dependent suite #239 was filed for.
    """
    env = dict(os.environ, XDG_CONFIG_HOME=str(HARNESS / ".no-such-config"))
    env.pop("QUARTERBACK_HARNESS_RULES", None)
    # No board: the mode is a property of the repo and must resolve without one.
    env.pop("QUARTERBACK_BASE_URL", None)
    env["QUARTERBACK_DIALS"] = ""
    return subprocess.run([sys.executable, str(QB_MODE), *args],
                          cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout with an origin, which `resolve_repo` requires to name a repo."""
    work = tmp_path / "myrepo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    git(work, "remote", "add", "origin", "https://github.com/acme/myrepo.git")
    (work / "README").write_text("x\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    return work


def declare(repo: Path, mode: dict) -> None:
    (repo / ".harness-rules.sample").write_text(json.dumps({"mode": mode}))


# ------------------------------------------------------------- the exit codes

def test_a_private_checkout_agrees_and_says_so(repo):
    """Primary, but nobody cuts worktrees from it — so it is not a shared tree
    and there is nothing to warn about. The false positive that would train a
    reader to skim the real ones."""
    r = run(cwd=repo)
    assert r.returncode == AGREES
    assert "CLEANROOM" in r.stdout
    assert r.stderr == ""


def test_the_shared_checkout_of_a_cleanroom_repo_exits_3(repo):
    (repo / ".worktree.json").write_text("{}\n")
    r = run(cwd=repo)
    assert r.returncode == VIOLATED
    # The remedy, not just the complaint — and on stderr, so a caller taking the
    # one-line answer off stdout is not handed a paragraph.
    assert "create-worktree" in r.stderr
    assert r.stdout.strip().startswith("⌂ CLEANROOM")


def test_a_worktree_of_that_same_repo_agrees(repo, tmp_path):
    (repo / ".worktree.json").write_text("{}\n")
    wt = tmp_path / "myrepo-side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert run(cwd=wt).returncode == AGREES


def test_a_jungle_repo_is_content_in_its_shared_checkout(repo):
    (repo / ".worktree.json").write_text("{}\n")
    declare(repo, {"name": "jungle"})
    r = run(cwd=repo)
    assert r.returncode == AGREES
    assert "~ JUNGLE" in r.stdout


def test_somewhere_that_is_not_a_checkout_cannot_tell(tmp_path):
    """Not 0. "This is fine" and "I could not ask" are different answers and a
    caller that spelled them the same way would report an unknown tree as safe."""
    r = run(cwd=tmp_path)
    assert r.returncode == CANNOT_TELL
    assert r.stdout == ""


def test_a_checkout_with_no_origin_cannot_tell_rather_than_crashing(tmp_path):
    """`resolve_repo` raises SystemExit for a repo with no `origin` — an ordinary
    local repo, and not a reason to take a status bar down."""
    solo = tmp_path / "solo"
    solo.mkdir()
    git(solo, "init", "-q", "-b", "main")
    r = run(cwd=solo)
    assert r.returncode == CANNOT_TELL
    assert "qb-mode" in r.stderr


# ------------------------------------------------------------ the output shapes

def test_it_answers_about_somewhere_else(repo, tmp_path):
    """The hook asks about the session's cwd from wherever it happens to run."""
    (repo / ".worktree.json").write_text("{}\n")
    r = run(str(repo), cwd=tmp_path)
    assert r.returncode == VIOLATED
    assert "CLEANROOM" in r.stdout


def test_bar_is_the_glyph_and_the_label_and_nothing_else(repo):
    """A status line renders this next to everything else it already shows, so it
    must not be handed a sentence."""
    (repo / ".worktree.json").write_text("{}\n")
    r = run("--bar", cwd=repo)
    assert r.stdout.strip() == "⌂ CLEANROOM"
    assert r.returncode == VIOLATED       # still the true answer, just not said


def test_quiet_says_nothing_at_all(repo):
    (repo / ".worktree.json").write_text("{}\n")
    r = run("--quiet", cwd=repo)
    assert (r.stdout, r.stderr, r.returncode) == ("", "", VIOLATED)


def test_a_mixed_repo_renders_both_halves(repo):
    """#178's "cleanroom tree, jungle plan". One word would have to lie."""
    declare(repo, {"name": "cleanroom", "landing": "direct"})
    r = run("--bar", cwd=repo)
    assert r.stdout.strip() == "⌂ CLEANROOM tree · JUNGLE plan"


def test_the_json_keeps_the_glyph_readable(repo):
    """`ensure_ascii` would hand every jq consumer `\\u2302` to decode itself."""
    r = run("--json", cwd=repo)
    assert '"⌂"' in r.stdout
    assert json.loads(r.stdout)["glyph"] == "⌂"


def test_the_json_carries_the_axes_not_just_the_name(repo):
    declare(repo, {"name": "jungle"})
    said = json.loads(run("--json", cwd=repo).stdout)
    assert said["isolation"] == "shared" and said["landing"] == "direct"
    assert said["violation"] is None


# --------------------------------------------- the coupling with the hook

def test_qb_hook_reads_only_fields_this_tool_emits(repo):
    """`qb-hook`'s note names JSON fields in a jq program. A renamed field there
    fails nothing and prints nothing, which reads exactly like a repo with no
    problem — the silence #178 exists to end. So the two are held together here.
    """
    emitted = set(json.loads(run("--json", cwd=repo).stdout))
    note = re.search(r"^_mode_note\(\).*?^}", QB_HOOK.read_text(), re.S | re.M)
    assert note, "qb-hook no longer defines _mode_note"
    # The jq PROGRAM alone, not the whole function: inside it every `.name` is a
    # field reference, so nothing has to be filtered by a vocabulary — and a
    # filter is exactly what would let a rename through, by discarding the one
    # spelling that no longer matches. Comments and shell words stay outside it.
    program = re.search(r"\|\s*jq\s+-r\s+'(.*?)'", note.group(0), re.S)
    assert program, "the note no longer pipes through a single-quoted jq program"
    read = set(re.findall(r"\.([a-z_]+)", program.group(1)))
    assert read, "the note stopped reading any field — did the jq program change?"
    assert read <= emitted, f"qb-hook reads fields qb-mode does not emit: {read - emitted}"


def test_qb_hook_calls_the_tool_by_name(repo):
    """It is on PATH by name, not by a path the hook computes: home-manager
    installs each bin file as its own flat store path, so a sibling lookup from
    the hook would resolve into /nix/store and find nothing."""
    body = QB_HOOK.read_text()
    assert "command -v qb-mode" in body
    assert "qb-mode --json" in body


# ------------------------------------------------------- and it asks no board

def test_it_does_not_wait_on_a_board_it_cannot_reach(repo):
    """The mode cannot come from a board dial — `BOARD_DIALS` is limited to
    judgements about cost and excludes the class this belongs to — so consulting
    one could only make the answer slower, never different. That matters because
    this runs at session start, where the dial layer's five-second timeout would
    be charged to every session on the fleet, and on a status line, where it would
    be charged again on every render.

    Pointed at a black hole rather than mocked: what is being asserted is that no
    request is made at all, and a stub board would answer instantly and prove
    nothing either way.
    """
    import time
    env_url = "http://192.0.2.1:9"          # TEST-NET-1, RFC 5737: goes nowhere
    started = time.monotonic()
    env = dict(os.environ, XDG_CONFIG_HOME=str(HARNESS / ".no-such-config"),
               QUARTERBACK_BASE_URL=env_url, QUARTERBACK_TOKEN="x")
    env.pop("QUARTERBACK_DIALS", None)      # the switch under test, left unset
    env.pop("QUARTERBACK_HARNESS_RULES", None)
    r = subprocess.run([sys.executable, str(QB_MODE)], cwd=str(repo),
                       capture_output=True, text=True, env=env, timeout=30)
    elapsed = time.monotonic() - started
    assert r.returncode == AGREES
    sys.path.insert(0, str(LOOPS))
    import harness_rules  # noqa: PLC0415 — the timeout it would have paid
    assert elapsed < harness_rules.DIALS_TIMEOUT, (
        f"took {elapsed:.1f}s — long enough to have waited on the board")
