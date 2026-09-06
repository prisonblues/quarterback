"""`prune-worktrees --finished`: which LIVE worktrees are done (#685).

Every other category in that script is about a worktree that is already gone. The
prior question — of the ones still registered, which can go — existed only as prose
in `/tree-shake`, re-derived by an agent each run and finished only with a human
present. So it ran rarely: on 2026-09-06 one lexray checkout carried 78 worktrees,
including a PR merged in July.

**The rule is wider than "PR merged", and the width is the point.** Measured on
that checkout, "merged" found one finished sibling in 29. Six more had landed by a
route GitHub records as CLOSED: a head reachable from a long-lived integration
branch that was not the PR's base (#700's `fca` case), or a closing comment saying
`Superseded by #N` where #N merged — the rebase-and-reopen pattern, whose old
patches are rewritten and so contained nowhere. Each of those has a test here, and
each has a twin pinning the boundary just past it: contained-elsewhere is finished,
closed-with-nothing is not; superseded-by-a-merge is finished, superseded-by-an-
open-PR is not.

**The third bucket is never folded into the other two.** `cannot-verify` — gh did
not answer, the head SHA is not fetched, the board could not be asked, there is no
branch to judge — is the script's own doctrine (#244, #735): a check that could not
run must not read as a check that passed, and here "passed" means `rm -rf`. Two of
the tests below exist only to hold that line: a failing `gh` is not in-progress,
and an absent SHA is not "nothing after the PR".

The block is extracted from the real script rather than copied, so a refactor that
moves or renames it fails here instead of leaving this suite green about code
nobody runs. One test at the end runs the whole script, because the driver around
the block — the create-name column, the porcelain rows, the walk over
`git worktree list` — is what `/tree-shake` actually reads.

Run: pytest harness/tests
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "prune-worktrees"

BASH = shutil.which("bash")
#: What the stanza shells out to. One BINARY at a time — `_path_sandbox` says why a
#: directory is the wrong unit.
TOOLS = ("git", "jq", "grep", "sed", "awk", "tail", "wc", "paste", "tr",
         "dirname", "basename", "cat", "head")
MISSING = [t for t in TOOLS if shutil.which(t) is None]

pytestmark = pytest.mark.skipif(
    BASH is None or MISSING,
    reason=f"bash and {TOOLS} must all be on PATH (missing: {MISSING})")

_START = "# >>> finished-classify"
_END = "# <<< finished-classify"


def classify_block() -> str:
    src = SCRIPT.read_text()
    assert _START in src and _END in src, (
        f"the {_START} / {_END} markers are gone from prune-worktrees, so this suite "
        "is asserting nothing — fix the markers rather than deleting the test")
    block = src.split(_START, 1)[1].split("\n", 1)[1].split(_END, 1)[0]
    assert "classify_worktree()" in block, "the markers no longer bracket the classifier"
    return block


#: A `gh` that answers from a JSON file instead of GitHub. Only the two calls the
#: classifier makes are implemented, and an unknown one exits 2 so that a new call
#: added to the script shows up here as a failure rather than as an empty answer.
FAKE_GH = """#!%(bash)s
db="${FAKE_GH_DB:?}"
[[ -n "${FAKE_GH_FAIL:-}" ]] && exit 1
cmd="$1 $2"; shift 2
case "$cmd" in
  "pr list")
    head=""
    while [[ $# -gt 0 ]]; do case $1 in --head) head=$2; shift 2 ;; *) shift ;; esac; done
    jq -c --arg h "$head" \
      '[.prs[] | select(.headRefName==$h)] | .[0:1] | map({number,state,headRefOid})' "$db" ;;
  "pr view")
    n=$1
    jq -c --argjson n "$n" \
      '.prs[] | select(.number==$n) | {state, comments:(.comments // [])}' "$db" ;;
  *) exit 2 ;;
esac
"""

#: A `worktree-holder` whose answer is chosen by the test: exit 3 and a holder
#: list, exit 4 for "could not tell", exit 0 otherwise.
FAKE_HOLDER = """#!%(bash)s
case "${FAKE_HOLDER_RC:-0}" in
  3) printf '%%s' '{"held":true,"holders":[{"holder":"zeus/amber-otter"}]}'; exit 3 ;;
  4) exit 4 ;;
  *) printf '%%s' '{"held":false,"holders":[]}'; exit 0 ;;
esac
"""


class Repo:
    """A main checkout with a bare origin, `test` and `fca` branches on both."""

    def __init__(self, tmp_path: Path, env: dict[str, str]):
        self.env = env
        self.origin = tmp_path / "origin.git"
        self.main = tmp_path / "proj"
        self.git("init", "-q", "--bare", str(self.origin), cwd=tmp_path)
        self.git("init", "-q", "-b", "test", str(self.main), cwd=tmp_path)
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.git("remote", "add", "origin", str(self.origin))
        (self.main / "README").write_text("base\n")
        self.git("add", "README")
        self.git("commit", "-q", "-m", "base")
        self.git("branch", "fca")
        self.git("push", "-q", "-u", "origin", "test", "fca")
        self.serial = 0

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.main, env=self.env,
            capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, wt: Path, text: str) -> str:
        self.serial += 1
        (wt / f"f{self.serial}").write_text(text + "\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", text, cwd=wt)
        return self.git("rev-parse", "HEAD", cwd=wt)

    def worktree(self, name: str, branch: str | None, *, base: str = "test",
                 commits: int = 1, push: bool = True, sibling: bool = True) -> Path:
        """A registered worktree at `<parent>/proj-<name>` (or elsewhere), on
        `branch` with `commits` commits past `base`, pushed unless told not to."""
        path = (self.main.parent / f"proj-{name}") if sibling \
            else (self.main.parent / "elsewhere" / name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if branch is None:
            self.git("worktree", "add", "-q", "--detach", str(path), base)
            return path
        self.git("worktree", "add", "-q", "-b", branch, str(path), base)
        for i in range(commits):
            self.commit(path, f"{branch} {i}")
        if push:
            self.git("push", "-q", "-u", "origin", branch, cwd=path)
        return path

    def head(self, path: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=path)


@pytest.fixture
def sandbox(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH % {"bash": BASH})
    gh.chmod(0o755)
    holder = bindir / "worktree-holder"
    holder.write_text(FAKE_HOLDER % {"bash": BASH})
    holder.chmod(0o755)
    env = _path_sandbox.sandbox_env(tmp_path, bindir, tools=TOOLS)
    env["FAKE_GH_DB"] = str(tmp_path / "gh.json")
    (tmp_path / "gh.json").write_text(json.dumps({"prs": []}))
    # git refuses to commit without an identity, and the sandbox has no ~/.gitconfig.
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    repo = Repo(tmp_path, env)
    return tmp_path, repo


def set_prs(sandbox, prs: list[dict]) -> None:
    tmp_path, _ = sandbox
    (tmp_path / "gh.json").write_text(json.dumps({"prs": prs}))


def classify(sandbox, path: Path, branch: str | None, *, locked: bool = False,
             holder: bool = True, **extra_env: str) -> tuple[str, str]:
    """Run the extracted block on one worktree -> (bucket, reason)."""
    tmp_path, repo = sandbox
    script = _path_sandbox.sibling_dir(tmp_path) / "classify.sh"
    script.write_text(
        "set -uo pipefail\n"
        f"MAIN_REPO={repo.main}\nPROJECT=proj\n"
        + ("HOLDER_BIN=$(command -v worktree-holder)\n" if holder else "HOLDER_BIN=''\n")
        + "GH_BIN=$(command -v gh 2>/dev/null || true)\n"
        + classify_block()
        + '\nclassify_worktree "$1" "$2" "$3"\n')
    env = dict(repo.env, **extra_env)
    got = subprocess.run(
        [BASH, str(script), str(path), branch or "", "true" if locked else "false"],
        cwd=repo.main, env=env, capture_output=True, text=True)
    assert got.returncode == 0, got.stderr
    lines = got.stdout.rstrip("\n").split("\n")
    assert len(lines) == 1, f"one row expected, got {lines!r}\n{got.stderr}"
    bucket, _, reason = lines[0].partition("\t")
    return bucket, reason


# --- merged ---------------------------------------------------------------------

def test_merged_pr_clean_and_at_its_head_is_finished(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/a",
                       "headRefOid": repo.head(wt)}])
    assert classify(sandbox, wt, "feat/a") == ("finished", "PR #7 merged")


def test_a_commit_after_the_pr_that_is_pushed_nowhere_blocks_the_teardown(sandbox):
    """`remove-worktree` deletes the local branch; this commit's only copy is on it."""
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    pr_head = repo.head(wt)
    repo.commit(wt, "post-merge tweak")
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/a",
                       "headRefOid": pr_head}])
    bucket, reason = classify(sandbox, wt, "feat/a")
    assert bucket == "in-progress"
    assert "1 commit after PR #7 not pushed" in reason


def test_a_commit_after_the_pr_that_is_pushed_does_not_block_it(sandbox):
    """`--not --remotes` is what makes this about loss rather than tidiness."""
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    pr_head = repo.head(wt)
    repo.commit(wt, "post-merge tweak")
    repo.git("push", "-q", "origin", "feat/a", cwd=wt)
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/a",
                       "headRefOid": pr_head}])
    assert classify(sandbox, wt, "feat/a")[0] == "finished"


# --- closed, which is where "merged" was wrong ----------------------------------

def test_closed_pr_whose_head_landed_on_another_branch_is_finished(sandbox):
    """#700: the work went into `fca`, which was not the PR's base, so GitHub says
    CLOSED. The code is reachable from a remote branch; that is what "landed" means."""
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    head = repo.head(wt)
    repo.git("checkout", "-q", "fca")
    repo.git("merge", "-q", "--no-ff", "-m", "merge feat/a into fca", "feat/a")
    repo.git("push", "-q", "origin", "fca")
    repo.git("checkout", "-q", "test")
    set_prs(sandbox, [{"number": 7, "state": "CLOSED", "headRefName": "feat/a",
                       "headRefOid": head}])
    assert classify(sandbox, wt, "feat/a") == (
        "finished", "PR #7 closed; its head is in origin/fca")


def test_the_branch_own_remote_ref_does_not_count_as_landed(sandbox):
    """`origin/feat/a` contains feat/a trivially. It is where the branch lives, not
    where it landed — and it is the only remote ref a closed, abandoned PR has."""
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [{"number": 7, "state": "CLOSED", "headRefName": "feat/a",
                       "headRefOid": repo.head(wt)}])
    bucket, reason = classify(sandbox, wt, "feat/a")
    assert bucket == "in-progress"
    assert "closed without merging" in reason and "a human decides" in reason


def test_closed_pr_superseded_by_a_merged_pr_is_finished(sandbox):
    """The rebase-and-reopen pattern: the old branch's patches were rewritten, so
    nothing contains them, and the closing comment is the only record."""
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [
        {"number": 7, "state": "CLOSED", "headRefName": "feat/a",
         "headRefOid": repo.head(wt),
         "comments": [{"body": "Two blockers here.\n\nSuperseded by #9, which carries "
                               "the live half rebased onto current `test`."}]},
        {"number": 9, "state": "MERGED", "headRefName": "feat/a-2",
         "headRefOid": "0" * 40},
    ])
    assert classify(sandbox, wt, "feat/a") == (
        "finished", "PR #7 closed, superseded by #9 (merged)")


def test_closed_pr_superseded_by_an_open_pr_is_in_progress(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [
        {"number": 7, "state": "CLOSED", "headRefName": "feat/a",
         "headRefOid": repo.head(wt),
         "comments": [{"body": "Replaced by #9."}]},
        {"number": 9, "state": "OPEN", "headRefName": "feat/a-2",
         "headRefOid": "0" * 40},
    ])
    assert classify(sandbox, wt, "feat/a") == (
        "in-progress", "PR #7 closed, superseded by #9 (OPEN)")


def test_the_last_supersession_in_the_thread_wins(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [
        {"number": 7, "state": "CLOSED", "headRefName": "feat/a",
         "headRefOid": repo.head(wt),
         "comments": [{"body": "Superseded by #8"}, {"body": "no — superseded by #9"}]},
        {"number": 8, "state": "OPEN", "headRefName": "x", "headRefOid": "0" * 40},
        {"number": 9, "state": "MERGED", "headRefName": "y", "headRefOid": "0" * 40},
    ])
    assert classify(sandbox, wt, "feat/a")[0] == "finished"


def test_open_pr_is_in_progress(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [{"number": 7, "state": "OPEN", "headRefName": "feat/a",
                       "headRefOid": repo.head(wt)}])
    assert classify(sandbox, wt, "feat/a") == ("in-progress", "PR #7 is open")


# --- no PR ----------------------------------------------------------------------

def test_no_pr_but_tip_reachable_from_a_remote_branch_is_finished(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "scratch", push=False)
    repo.git("checkout", "-q", "test")
    repo.git("merge", "-q", "--ff-only", "scratch")
    repo.git("push", "-q", "origin", "test")
    assert classify(sandbox, wt, "scratch") == ("finished", "no PR; tip is in origin/test")


def test_no_pr_and_an_uncontained_tip_is_in_progress(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "scratch", push=False)
    assert classify(sandbox, wt, "scratch") == (
        "in-progress", "no PR, and the tip is in no remote branch")


# --- cannot-verify is its own answer --------------------------------------------

def test_gh_failing_is_cannot_verify_not_in_progress(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    bucket, reason = classify(sandbox, wt, "feat/a", FAKE_GH_FAIL="1")
    assert bucket == "cannot-verify"
    assert "gh pr list" in reason


def test_gh_absent_is_cannot_verify(sandbox):
    tmp_path, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    (tmp_path / "bin" / "gh").unlink()
    bucket, reason = classify(sandbox, wt, "feat/a")
    assert bucket == "cannot-verify"
    assert "not installed" in reason


def test_a_head_sha_this_repo_does_not_have_is_cannot_verify(sandbox):
    """With the object absent `git log` fails and a bare `| wc -l` reads 0 —
    "nothing to lose" on exactly the branch nobody can vouch for."""
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/a",
                       "headRefOid": "deadbeef" * 5}])
    bucket, reason = classify(sandbox, wt, "feat/a")
    assert bucket == "cannot-verify"
    assert "deadbeef" in reason and "fetch" in reason


def test_a_board_that_could_not_be_asked_is_cannot_verify(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/a",
                       "headRefOid": repo.head(wt)}])
    bucket, reason = classify(sandbox, wt, "feat/a", FAKE_HOLDER_RC="4")
    assert bucket == "cannot-verify"
    assert "board" in reason


def test_detached_head_is_cannot_verify(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", None)
    bucket, reason = classify(sandbox, wt, None)
    assert bucket == "cannot-verify"
    assert "detached" in reason


# --- in-progress for reasons that are not the PR --------------------------------

def test_a_held_tree_is_in_progress_and_names_the_holder(sandbox):
    """A merged PR does not mean nobody is in there."""
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/a",
                       "headRefOid": repo.head(wt)}])
    assert classify(sandbox, wt, "feat/a", FAKE_HOLDER_RC="3") == (
        "in-progress", "held by a live agent: zeus/amber-otter")


def test_no_worktree_holder_installed_means_nobody_to_find_not_unknown(sandbox):
    """No harness on the host is no board being written to. A `NOT CHECKED` on
    every row of a repo that has no board would be the noise the claim sweep was
    careful to avoid — the same call it makes."""
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/a",
                       "headRefOid": repo.head(wt)}])
    assert classify(sandbox, wt, "feat/a", holder=False)[0] == "finished"


def test_a_dirty_tree_is_in_progress_whatever_its_pr_says(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/a",
                       "headRefOid": repo.head(wt)}])
    (wt / "stray").write_text("x\n")
    (wt / "README").write_text("edited\n")
    assert classify(sandbox, wt, "feat/a") == (
        "in-progress", "2 uncommitted or untracked paths")


def test_a_locked_tree_is_in_progress(sandbox):
    _, repo = sandbox
    wt = repo.worktree("a", "feat/a")
    assert classify(sandbox, wt, "feat/a", locked=True)[0] == "in-progress"


# --- the driver: what /tree-shake reads -----------------------------------------

def test_the_whole_script_reports_every_registered_tree_with_its_teardown(sandbox):
    """End to end, `--finished --porcelain`: one row per registered worktree, the
    main checkout excluded, and the create-name column set only for a
    `<parent>/<project>-<name>` sibling — an agent tree under `.claude/worktrees`
    was never given containers, a database or a port, and `/tree-shake` has to
    know which teardown to run."""
    tmp_path, repo = sandbox
    sib = repo.worktree("done", "feat/done")
    agent = repo.worktree("agent-1", "review/x", sibling=False)
    set_prs(sandbox, [{"number": 7, "state": "MERGED", "headRefName": "feat/done",
                       "headRefOid": repo.head(sib)}])
    got = subprocess.run(
        [BASH, str(SCRIPT), "--finished", "--porcelain", "--project", "proj"],
        cwd=repo.main, env=repo.env, capture_output=True, text=True)
    assert got.returncode == 0, got.stderr + got.stdout
    rows = {}
    for line in got.stdout.splitlines():
        if "\t" not in line:
            continue
        bucket, path, branch, cname, reason = line.split("\t")
        rows[path] = (bucket, branch, cname, reason)
    assert str(repo.main) not in rows, "the main checkout is not a candidate"
    assert rows[str(sib)] == ("finished", "feat/done", "done", "PR #7 merged")
    assert rows[str(agent)] == (
        "in-progress", "review/x", "-", "no PR, and the tip is in no remote branch")


def test_a_destructive_flag_beside_finished_is_refused(sandbox):
    _, repo = sandbox
    got = subprocess.run(
        [BASH, str(SCRIPT), "--finished", "--remove-dirs"],
        cwd=repo.main, env=repo.env, capture_output=True, text=True)
    assert got.returncode != 0
    assert "--finished is a report" in got.stderr
