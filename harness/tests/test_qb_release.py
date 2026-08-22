"""`qb-release` — handing a checkout claim back (#337).

`create-worktree` takes a `kind=work` claim on the issue a branch names, held by
the MACHINE with no session and an 8h TTL, and until this existed nothing gave it
back when the work landed. #277's `stop` half releases a *session's* claims and
these have none — they were taken by a script, on behalf of a worktree, before
the agent that would use it existed. So the only thing that ever freed one was
the TTL: measured on 2026-08-22, four plan items still carried live claims after
their PRs had merged, one of them shipped as v2.78 hours earlier.

Three properties, and the second is the one that keeps it usable on a teardown
path:

* the resource is NAMED and the board derives the key (#172). A release that
  composed `work/<repo>#337` for itself would be a second spelling waiting to
  disagree with the one `qb-claim` wrote.
* NOTHING TO RELEASE IS SUCCESS. Three callers release one claim by design — the
  land step, the worktree teardown, and `prune-worktrees` — so the second and
  third must not report failure for finding the work already done.
* somebody else's claim is exit 1 and names them, exactly as `qb-claim`'s hold
  does; a board that cannot be reached is exit 2. Collapsing those is how "this
  is not yours" gets reported as "try again later" forever.

Stubbed the way `test_qb_end.py` stubs one: a COPY of the script beside a stub
`qbdata.py`. The script inserts its own directory at the front of `sys.path` —
which is how an installed harness finds the module beside it in `$out/bin` — so
PYTHONPATH cannot shadow the real one and the copy is the only seam there is.

Run: pytest harness/tests
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
RELEASE = BIN / "qb-release"

RELEASED, REFUSED, UNKNOWN = 0, 1, 2


def run(*args, tmp_path: Path, claims: list | None = None, board: bool = True,
        repo: str | None = "acme/widget", release_status: int = 200,
        get_status: int = 200, cwd: Path | None = None,
        session_env: str | None = None):
    """Run `qb-release` against a stubbed board that answers with `claims`."""
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    copied = stub / RELEASE.name
    copied.write_bytes(RELEASE.read_bytes())
    (stub / "qbdata.py").write_text(f"""
import io, json, sys, urllib.error

BOARD = {board!r}
REPO = {repo!r}
CLAIMS = json.loads({json.dumps(claims if claims is not None else [])!r})
GET_STATUS = {get_status!r}
RELEASE_STATUS = {release_status!r}


def _fail(path, status, detail):
    raise urllib.error.HTTPError(
        "http://board" + path, status, "nope", {{}},
        io.BytesIO(json.dumps({{"detail": detail}}).encode()))


class _Client:
    def get(self, path, params=None):
        print(json.dumps({{"verb": "get", "path": path, "params": params}}),
              file=sys.stderr)
        if GET_STATUS != 200:
            _fail(path, GET_STATUS, {{"error": "no"}})
        return {{"claims": CLAIMS}}

    def post(self, path, body):
        print(json.dumps({{"verb": "post", "path": path, "body": body}}),
              file=sys.stderr)
        if RELEASE_STATUS != 200:
            _fail(path, RELEASE_STATUS, {{"error": "not your claim"}})
        return {{"released": True}}


def repo_slug(path="."):
    return REPO


def board_client():
    if not BOARD:
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset)")
    return _Client(), None
""")
    env = {**os.environ}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if session_env is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_env
    return subprocess.run([sys.executable, str(copied), *args],
                          capture_output=True, text=True, env=env,
                          cwd=str(cwd) if cwd else None)


def calls(got) -> list[dict]:
    """Every request the stub saw, in order."""
    return [json.loads(ln) for ln in got.stderr.splitlines()
            if ln.startswith('{"verb"')]


LIVE = [{"claim_id": "c-1", "kind": "work", "key": "acme/widget#337",
         "holder": "zeus", "session": None, "note": "worktree feat/issue-337 on zeus",
         "expires": "2026-08-23T00:00:00+00:00"}]


# ------------------------------------------------ the resource is named, not keyed

def test_the_lookup_names_the_resource_and_lets_the_board_key_it(tmp_path):
    """#172's whole rule, on the read that finds the row to release. A client that
    composed `work/acme/widget#337` would look up a spelling nothing wrote and
    report, truthfully and uselessly, that there was nothing to release."""
    got = run("issue", "337", tmp_path=tmp_path, claims=LIVE)
    assert got.returncode == RELEASED, got.stderr
    lookup = calls(got)[0]
    assert lookup["path"] == "/claims"
    assert lookup["params"] == {"ref_kind": "issue", "ref_value": "337",
                                "repo": "acme/widget"}
    assert "key" not in lookup["params"] and "kind" not in lookup["params"]


def test_the_claim_the_board_named_is_the_one_released(tmp_path):
    got = run("issue", "337", tmp_path=tmp_path, claims=LIVE)
    assert got.returncode == RELEASED
    post = [c for c in calls(got) if c["verb"] == "post"]
    assert len(post) == 1
    assert post[0]["path"] == "/claim/release"
    assert post[0]["body"]["claim_id"] == "c-1"
    assert "acme/widget#337" in got.stderr


def test_a_pr_is_released_by_its_own_kind(tmp_path):
    got = run("pr", "347", tmp_path=tmp_path, claims=LIVE)
    assert got.returncode == RELEASED
    assert calls(got)[0]["params"]["ref_kind"] == "pr"


def test_a_board_object_carries_no_repo(tmp_path):
    """A plan or an item is identified by a board id, so sending a repo alongside
    would give one row two keys depending on where the caller was standing."""
    got = run("item", "1e4a", tmp_path=tmp_path, claims=[])
    assert got.returncode == RELEASED
    assert calls(got)[0]["params"]["repo"] is None


# ------------------------------------------------------ nothing to release is 0

def test_nothing_held_is_success_and_posts_nothing(tmp_path):
    """Three callers release one claim by design — the land step, the teardown and
    `prune-worktrees`. The second and third find the work already done, and a
    teardown that failed because of that would be a teardown nobody could trust."""
    got = run("issue", "337", tmp_path=tmp_path, claims=[])
    assert got.returncode == RELEASED, got.stderr
    assert not [c for c in calls(got) if c["verb"] == "post"]
    assert "nothing" in got.stderr


# ------------------------------------------------- the number comes off the branch

@pytest.mark.parametrize("branch,number", [
    ("feat/issue-337", "337"),
    ("fix/issue-114", "114"),
    ("feat/issue-135-qb-next", "135"),
    ("issue-42", "42"),
])
def test_the_issue_number_is_read_out_of_a_branch_name(branch, number, tmp_path):
    """The same derivation `create-worktree` claims by, because this is the
    release of what that took. A `--issue` flag would be a second place the number
    is written, and releasing somebody else's claim is worse than releasing none."""
    got = run("--branch", branch, tmp_path=tmp_path, claims=LIVE)
    assert got.returncode == RELEASED, got.stderr
    assert calls(got)[0]["params"]["ref_value"] == number


@pytest.mark.parametrize("branch", [
    "chore/review-policy-p1-p2", "main", "feat/issue-172x", "feat/reissue-5",
])
def test_a_branch_that_names_no_issue_releases_nothing(branch, tmp_path):
    got = run("--branch", branch, tmp_path=tmp_path, claims=LIVE)
    assert got.returncode == RELEASED, got.stderr
    assert calls(got) == [], "asked the board about a branch that names no issue"
    assert "names no issue" in got.stderr


def test_with_no_argument_at_all_it_reads_the_checkouts_branch(tmp_path):
    """What the land step runs from inside the worktree: `qb-release`, no number
    typed anywhere."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "feat/issue-337"],
                ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True,
                       capture_output=True)
    got = run(tmp_path=tmp_path, claims=LIVE, cwd=repo)
    assert got.returncode == RELEASED, got.stderr
    assert calls(got)[0]["params"]["ref_value"] == "337"


def test_a_kind_with_no_value_is_refused_rather_than_guessed(tmp_path):
    got = run("issue", tmp_path=tmp_path, claims=LIVE)
    assert got.returncode == UNKNOWN
    assert calls(got) == []


# --------------------------------------------------- somebody else's is not ours

def test_a_claim_that_is_not_ours_is_exit_1_and_names_the_holder(tmp_path):
    """A 403 is the ownership rule working, not a caller error: a claim stamped
    with another session on this same box belongs to that session."""
    got = run("issue", "337", tmp_path=tmp_path, claims=LIVE, release_status=403)
    assert got.returncode == REFUSED
    assert "refused" in got.stderr and "zeus" in got.stderr


def test_an_unreachable_board_is_exit_2_not_exit_1(tmp_path):
    """"Somebody has it" is a thing to act on and "the board is down" is a thing
    to wait out; a caller that collapsed them would report one as the other."""
    got = run("issue", "337", tmp_path=tmp_path, board=False)
    assert got.returncode == UNKNOWN
    assert "could not reach the board" in got.stderr


def test_a_board_that_refuses_the_lookup_is_unknown(tmp_path):
    got = run("issue", "337", tmp_path=tmp_path, get_status=422)
    assert got.returncode == UNKNOWN
    assert "not an outage" in got.stderr


def test_a_repo_that_cannot_be_named_is_unknown(tmp_path):
    """A claim is keyed on the repo, and guessing one from a directory name is how
    two agents claimed one issue under two keys."""
    got = run("issue", "337", tmp_path=tmp_path, repo=None)
    assert got.returncode == UNKNOWN
    assert "owner/name" in got.stderr


# ------------------------------------------------------------- the session it sends

def test_the_session_is_sent_so_an_agent_can_release_its_own(tmp_path):
    """`may_mutate` requires a recorded session to match. The checkout claim names
    none and falls back to the machine, but a claim taken through the `claim` tool
    is owned by the session that took it — which must be able to hand it back."""
    got = run("issue", "337", tmp_path=tmp_path, claims=LIVE,
              session_env="sess-abc")
    post = [c for c in calls(got) if c["verb"] == "post"][0]
    assert post["body"]["session"] == "sess-abc"


def test_no_session_in_the_environment_sends_none(tmp_path):
    """A session key of `""` is not the same as absent, and only one of them is
    storable — the checkout claim's is absent."""
    got = run("issue", "337", tmp_path=tmp_path, claims=LIVE)
    post = [c for c in calls(got) if c["verb"] == "post"][0]
    assert "session" not in post["body"]


def test_quiet_says_nothing_at_all(tmp_path):
    got = run("issue", "337", "--quiet", tmp_path=tmp_path, claims=LIVE)
    assert got.returncode == RELEASED
    assert got.stdout == ""
    assert [ln for ln in got.stderr.splitlines() if not ln.startswith('{"verb"')] == []
